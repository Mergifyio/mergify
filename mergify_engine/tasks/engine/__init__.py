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

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.tasks.engine import actions_runner
from mergify_engine.tasks.engine import commands_runner
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def get_github_pull_from_sha(repo, sha):
    for pull in repo.get_pulls():
        if pull.head.sha == sha:
            return pull


def get_github_pull_from_event(repo, event_type, data):
    if "pull_request" in data and data["pull_request"]:
        return github.PullRequest.PullRequest(
            repo._requester, {}, data["pull_request"], completed=True
        )

    elif event_type == "issue_comment":
        try:
            return repo.get_pull(data["issue"]["number"])
        except github.UnknownObjectException:  # pragma: no cover
            pass

    elif event_type == "status":
        return get_github_pull_from_sha(repo, data["sha"])

    elif event_type in ["check_suite", "check_run"]:
        if event_type == "check_run":
            pulls = data["check_run"]["check_suite"]["pull_requests"]
            sha = data["check_run"]["head_sha"]
        else:
            pulls = data["check_suite"]["pull_requests"]
            sha = data["check_suite"]["head_sha"]
        if not pulls:
            return get_github_pull_from_sha(repo, sha)
        if len(pulls) > 1:  # pragma: no cover
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning(
                "check_suite/check_run attached on multiple pulls",
                gh_owner=repo.owner.login,
                gh_repo=repo.name,
                event_type=event_type,
            )

        for p in pulls:
            try:
                pull = repo.get_pull(p["number"])
            except github.UnknownObjectException:  # pragma: no cover
                continue

            if pull and not pull.merged:
                return pull


def check_configuration_changes(event_pull):
    if event_pull.base.repo.default_branch == event_pull.base.ref:
        ref = None
        for f in event_pull.get_files():
            if f.filename in rules.MERGIFY_CONFIG_FILENAMES:
                ref = f.contents_url.split("?ref=")[1]

        if ref is not None:
            try:
                rules.get_mergify_config(event_pull.base.repo, ref=ref)
            except rules.InvalidRules as e:  # pragma: no cover
                # Not configured, post status check with the error message
                # TODO(sileht): we can annotate the .mergify.yml file in Github
                # UI with that API
                check_api.set_check_run(
                    event_pull,
                    "Summary",
                    "completed",
                    "failure",
                    output={
                        "title": "The new Mergify configuration is invalid",
                        "summary": str(e),
                    },
                )
            else:
                check_api.set_check_run(
                    event_pull,
                    "Summary",
                    "completed",
                    "success",
                    output={
                        "title": "The new Mergify configuration is valid",
                        "summary": "This pull request must be merged "
                        "manually because it modifies Mergify configuration",
                    },
                )

            return True
    return False


def copy_summary_from_previous_head_sha(g_pull, sha):
    checks = check_api.get_checks_for_ref(
        g_pull.base.repo,
        sha,
        {"check_name": actions_runner.SUMMARY_NAME},
        mergify_only=True,
    )
    if not checks:
        g_pull.log.warning(
            "Got synchronize event but didn't find Summary on previous head sha",
        )
        return
    check_api.set_check_run(
        g_pull,
        actions_runner.SUMMARY_NAME,
        "completed",
        "success",
        output={
            "title": checks[0].output["title"],
            "summary": checks[0].output["summary"],
        },
    )


@app.task
def run(event_type, data):
    """Everything starts here."""
    installation_id = data["installation"]["id"]
    installation_token = utils.get_installation_token(installation_id)
    if not installation_token:
        return

    g = github.Github(
        installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
    )

    if config.LOG_RATELIMIT:  # pragma: no cover
        rate = g.get_rate_limit().rate
        LOG.info(
            "ratelimit: %s/%s, reset at %s",
            rate.remaining,
            rate.limit,
            rate.reset,
            event_type=event_type,
            gh_owner=data["repository"]["owner"]["login"],
            gh_repo=data["repository"]["name"],
        )

    try:
        repo = g.get_repo(
            data["repository"]["owner"]["login"] + "/" + data["repository"]["name"]
        )
    except github.UnknownObjectException:  # pragma: no cover
        LOG.info(
            "Repository not found in the event %s, ignoring",
            event_type,
            gh_owner=data["repository"]["owner"]["login"],
            gh_repo=data["repository"]["name"],
        )
        return

    event_pull = get_github_pull_from_event(repo, event_type, data)

    if not event_pull:  # pragma: no cover
        LOG.info(
            "No pull request found in the event %s, " "ignoring",
            event_type,
            gh_owner=data["repository"]["owner"]["login"],
            gh_repo=data["repository"]["name"],
        )
        return

    # Override pull_request with the updated one
    data["pull_request"] = event_pull.raw_data

    event_pull.log.info("Pull request found in the event %s", event_type)

    if (
        "base" not in event_pull.raw_data
        or "repo" not in event_pull.raw_data["base"]
        or len(list(event_pull.raw_data["base"]["repo"].keys())) < 70
    ):
        event_pull.log.warning(
            "the pull request payload looks suspicious",
            event_type=event_type,
            data=data,
        )

    if (
        event_type == "status" and event_pull.head.sha != data["sha"]
    ):  # pragma: no cover
        event_pull.log.info("No need to proceed queue (got status of an old commit)",)
        return

    elif (
        event_type in ["status", "check_suite", "check_run"] and event_pull.merged
    ):  # pragma: no cover
        event_pull.log.info(
            "No need to proceed queue (got status of a merged " "pull request)",
        )
        return
    elif (
        event_type in ["check_suite", "check_run"]
        and event_pull.head.sha != data[event_type]["head_sha"]
    ):  # pragma: no cover
        event_pull.log.info(
            "No need to proceed queue (got %s of an old " "commit)", event_type,
        )
        return

    if check_configuration_changes(event_pull):
        event_pull.log.info("Configuration changed, ignoring",)
        return

    # BRANCH CONFIGURATION CHECKING
    try:
        mergify_config = rules.get_mergify_config(repo)
    except rules.NoRules:  # pragma: no cover
        event_pull.log.info("No need to proceed queue (.mergify.yml is missing)",)
        return
    except rules.InvalidRules as e:  # pragma: no cover
        # Not configured, post status check with the error message
        if event_type == "pull_request" and data["action"] in ["opened", "synchronize"]:
            check_api.set_check_run(
                event_pull,
                "Summary",
                "completed",
                "failure",
                output={
                    "title": "The Mergify configuration is invalid",
                    "summary": str(e),
                },
            )
        return

    subscription = sub_utils.get_subscription(
        utils.get_redis_for_cache(), installation_id
    )

    if repo.private and not subscription["subscription_active"]:
        check_api.set_check_run(
            event_pull,
            "Summary",
            "completed",
            "failure",
            output={
                "title": "Mergify is disabled",
                "summary": subscription["subscription_reason"],
            },
        )
        return

    # CheckRun are attached to head sha, so when user add commits or force push
    # we can't directly get the previous Mergify Summary. So we copy it here, then
    # anything that looks at it in next celery tasks will find it.
    if event_type == "pull_request" and data["action"] == "synchronize":
        copy_summary_from_previous_head_sha(event_pull, data["before"])

    commands_runner.spawn_pending_commands_tasks(
        installation_id, event_type, data, event_pull
    )

    if event_type == "issue_comment":
        commands_runner.run_command.s(
            installation_id, event_type, data, data["comment"]["body"]
        ).apply_async()
    else:
        actions_runner.handle.s(
            installation_id,
            mergify_config["pull_request_rules"].as_dict(),
            event_type,
            data,
        ).apply_async()
