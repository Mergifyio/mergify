# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.tasks import app
from mergify_engine.tasks import mergify_events


LOG = logs.getLogger(__name__)


@app.task
def job_marketplace(event_type, event_id, data):

    owner = data["marketplace_purchase"]["account"]["login"]
    account_type = data["marketplace_purchase"]["account"]["type"]
    try:
        installation = github_app.get_client().get_installation(
            owner, account_type=account_type
        )
    except exceptions.MergifyNotInstalled:
        return

    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation["id"])

    LOG.info(
        "Marketplace event",
        event_type=event_type,
        event_id=event_id,
        install_id=installation["id"],
        sender=data["sender"]["login"],
        gh_owner=owner,
    )


def get_extra_msg_from_event(event_type, data):
    if event_type == "pull_request":
        return ", action: %s" % data["action"]

    elif event_type == "pull_request_review":
        return ", action: %s, review-state: %s" % (
            data["action"],
            data["review"]["state"],
        )

    elif event_type == "pull_request_review_comment":
        return ", action: %s, review-state: %s" % (
            data["action"],
            data["comment"]["position"],
        )

    elif event_type == "status":
        return ", ci-status: %s, sha: %s" % (data["state"], data["sha"])

    elif event_type in ["check_run", "check_suite"]:
        return ", action: %s, status: %s, conclusion: %s, sha: %s" % (
            data["action"],
            data[event_type]["status"],
            data[event_type]["conclusion"],
            data[event_type]["head_sha"],
        )
    else:
        return ""


def get_ignore_reason(subscription, event_type, data):
    if "installation" not in data:
        return "ignored (no installation id)"

    elif not subscription["tokens"]:
        return "ignored (no token)"

    elif event_type in ["installation", "installation_repositories"]:
        return "ignored (action %s)" % data["action"]

    elif event_type in ["push"] and not data["ref"].startswith("refs/heads/"):
        return "ignored (push on %s)" % data["ref"]

    elif event_type == "status" and data["state"] == "pending":
        return "ignored (state pending)"

    elif event_type == "check_suite" and data["action"] != "rerequested":
        return "ignored (check_suite/%s)" % data["action"]

    elif (
        event_type in ["check_run", "check_suite"]
        and data[event_type]["app"]["id"] == config.INTEGRATION_ID
        and data["action"] != "rerequested"
    ):
        return "ignored (mergify %s)" % event_type

    elif event_type == "issue_comment" and data["action"] != "created":
        return "ignored (comment have been %s)" % data["action"]

    elif (
        event_type == "issue_comment"
        and "@mergify " not in data["comment"]["body"].lower()
        and "@mergifyio " not in data["comment"]["body"].lower()
    ):
        return "ignored (comment is not for mergify)"

    if "repository" in data and data["repository"]["archived"]:  # pragma: no cover
        return "ignored (repository archived)"

    elif event_type not in [
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "push",
        "status",
        "check_suite",
        "check_run",
        "refresh",
    ]:
        return "ignored (unexpected event_type)"


def meter_event(event_type, data):
    tags = [f"event_type:{event_type}"]

    if "action" in data:
        tags.append(f"action:{data['action']}")

    if (
        event_type == "pull_request"
        and data["action"] == "closed"
        and data["pull_request"]["merged"]
    ):
        if data["pull_request"]["merged_by"] and data["pull_request"]["merged_by"][
            "login"
        ] in ["mergify[bot]", "mergify-test[bot]"]:
            tags.append("by_mergify")

    statsd.increment("github.events", tags=tags)


def _get_github_pulls_from_sha(client, sha):
    for pull in client.items("pulls"):
        if pull["head"]["sha"] == sha:
            return [pull]
    return []


def _extract_pulls_from_event(installation_id, owner, repo, event_type, data):
    if "pull_request" in data and data["pull_request"]:
        return [data["pull_request"]]

    try:
        installation = github.get_installation(owner, repo, installation_id)
    except exceptions.MergifyNotInstalled:
        return []

    with github.get_client(owner, repo, installation) as client:
        if event_type == "issue_comment":
            try:
                return [client.item(f"pulls/{data['issue']['number']}")]
            except httpx.HTTPNotFound:  # pragma: no cover
                return []
        elif event_type == "status":
            return _get_github_pulls_from_sha(client, data["sha"])
        elif event_type in ["check_suite", "check_run"]:
            # NOTE(sileht): This list may contains Pull Request from another org/user fork...
            base_repo_url = str(client.base_url)[:-1]
            pulls = data[event_type]["pull_requests"]
            pulls = [p for p in pulls if p["base"]["repo"]["url"] == base_repo_url]
            if not pulls:
                sha = data[event_type]["head_sha"]
                pulls = _get_github_pulls_from_sha(client, sha)
            return pulls

    return []


def _filter_event_data(event_type, data):
    slim_data = {"sender": data["sender"]}

    # For pull_request opened/synchronise/closed
    # and refresh event
    for attr in ("action", "after", "before"):
        if attr in data:
            slim_data[attr] = data[attr]

    # For commands runner
    if event_type == "issue_comment":
        slim_data["comment"] = data["comment"]
    return slim_data


@app.task
def job_filter_and_dispatch(event_type, event_id, data):
    meter_event(event_type, data)

    if "installation" in data:
        installation_id = data["installation"]["id"]
        r = utils.get_redis_for_cache()
        if event_type in ["installation", "installation_repositories"]:
            r.delete("subscription-cache-%s" % installation_id)
        subscription = sub_utils.get_subscription(r, installation_id)
    else:
        installation_id = "Unknown"
        subscription = {
            "subscription_active": False,
            "subscription_reason": "No installation for this event",
            "tokens": None,
        }

    reason = get_ignore_reason(subscription, event_type, data)
    if reason:
        msg_action = reason
    elif event_type in ["push"]:
        owner, _, repo = data["repository"]["full_name"].partition("/")
        branch = data["ref"][11:]
        msg_action = "run refresh branch %s" % branch
        mergify_events.job_refresh.s(owner, repo, "branch", branch).apply_async()
    else:
        owner = data["repository"]["owner"]["login"]
        repo = data["repository"]["name"]

        pulls = _extract_pulls_from_event(
            installation_id, owner, repo, event_type, data
        )

        if pulls:
            slim_data = _filter_event_data(event_type, data)
            for pull in pulls:
                worker.push(
                    installation_id, owner, repo, pull["number"], event_type, slim_data
                )
            msg_action = "pushed to backend%s" % get_extra_msg_from_event(
                event_type, data
            )
        else:  # pragma: no cover
            msg_action = "ignored (no pull request found in the event %s%s" % (
                event_type,
                get_extra_msg_from_event(event_type, data),
            )

    if "repository" in data:
        owner, _, repo_name = data["repository"]["full_name"].partition("/")
        private = data["repository"]["private"]
    else:
        owner = "Unknown"
        repo_name = "Unknown"
        private = "Unknown"

    LOG.info(
        "GithubApp event %s",
        msg_action,
        event_type=event_type,
        event_id=event_id,
        install_id=installation_id,
        sender=data["sender"]["login"],
        gh_owner=owner,
        gh_repo=repo_name,
        gh_private=private,
        subscription_active=subscription["subscription_active"],
        subscription_reason=subscription["subscription_reason"],
    )
