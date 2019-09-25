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

import daiquiri

import github


from mergify_engine import config
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.tasks import engine
from mergify_engine.tasks import mergify_events
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


@app.task
def job_marketplace(event_type, event_id, data):

    owner = data["marketplace_purchase"]["account"]["login"]
    account_type = data["marketplace_purchase"]["account"]["type"]
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    try:
        installation_id = utils.get_installation_id(
            integration, owner, account_type=account_type
        )
    except github.GithubException as e:
        LOG.warning("%s: mergify not installed", owner, error=str(e))
        installation_id = None
        subscription = "Unknown"
    else:
        r = utils.get_redis_for_cache()
        r.delete("subscription-cache-%s" % installation_id)
        subscription = sub_utils.get_subscription(r, installation_id)

    LOG.info('Marketplace event',
             event_type=event_type,
             event_id=event_id,
             install_id=installation_id,
             sender=data["sender"]["login"],
             subscription_active=subscription["subscription_active"],
             subscription_reason=subscription["subscription_reason"])


@app.task
def job_filter_and_dispatch(event_type, event_id, data):
    if "installation" not in data:
        subscription = {"subscription_active": "Unknown",
                        "subscription_reason": "No"}
    else:
        subscription = sub_utils.get_subscription(
            utils.get_redis_for_cache(), data["installation"]["id"])

    if "installation" not in data:
        msg_action = "ignored (no installation id)"

    elif not subscription["tokens"]:
        msg_action = "ignored (no token)"

    elif event_type in ["installation", "installation_repositories"]:
        msg_action = "ignored (action %s)" % data["action"]

    elif event_type in ["push"]:
        repo_name = data["repository"]["full_name"]
        owner, _, repo = repo_name.partition("/")
        if data["ref"].startswith("refs/heads/"):
            branch = data["ref"][11:]
            msg_action = "run refresh branch %s" % branch
            mergify_events.job_refresh.s(
                owner, repo, "branch", branch
            ).apply_async(countdown=10)
        else:
            msg_action = "ignored (push on %s)" % branch

    elif event_type in ["pull_request", "pull_request_review", "status",
                        "check_suite", "check_run", "refresh"]:

        if data["repository"]["archived"]:  # pragma: no cover
            msg_action = "ignored (repository archived)"

        elif event_type == "status" and data["state"] == "pending":
            msg_action = "ignored (state pending)"

        elif (event_type in ["check_run", "check_suite"] and
              data[event_type]["app"]["id"] == config.INTEGRATION_ID and
              data["action"] != "rerequested"):
            msg_action = "ignored (mergify %s)" % event_type

        elif (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize",
                "labeled", "unlabeled", "edited", "locked", "unlocked"]):
            msg_action = "ignored (action %s)" % data["action"]

        else:
            engine.run.s(event_type, data).apply_async()
            msg_action = "pushed to backend"

            if event_type == "pull_request":
                msg_action += ", action: %s" % data["action"]

            elif event_type == "pull_request_review":
                msg_action += ", action: %s, review-state: %s" % (
                    data["action"], data["review"]["state"])

            elif event_type == "pull_request_review_comment":
                msg_action += ", action: %s, review-state: %s" % (
                    data["action"], data["comment"]["position"])

            elif event_type == "status":
                msg_action += ", ci-status: %s, sha: %s" % (
                    data["state"], data["sha"])

            elif event_type in ["check_run", "check_suite"]:
                msg_action += (
                    ", action: %s, status: %s, conclusion: %s, sha: %s" % (
                        data["action"],
                        data[event_type]["status"],
                        data[event_type]["conclusion"],
                        data[event_type]["head_sha"]))
    else:
        msg_action = "ignored (unexpected event_type)"

    if "repository" in data:
        installation_id = data["installation"]["id"]
        repo_name = data["repository"]["full_name"]
        private = data["repository"]["private"]
    elif "installation" in data:
        installation_id = data["installation"]["id"],
        repo_name = data["installation"]["account"]["login"]
        private = "Unknown yet"
    else:
        installation_id = "Unknown"
        repo_name = "Unknown"
        private = "Unknown"

    LOG.info('GithubApp event %s', msg_action,
             event_type=event_type,
             event_id=event_id,
             install_id=installation_id,
             sender=data["sender"]["login"],
             repository=repo_name,
             private=private,
             subscription_active=subscription["subscription_active"],
             subscription_reason=subscription["subscription_reason"])
