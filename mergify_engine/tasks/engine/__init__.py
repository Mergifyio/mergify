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
from mergify_engine.tasks.engine import v2
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def get_github_pull_from_sha(repo, sha):
    for pull in repo.get_pulls():
        if pull.head.sha == sha:
            return pull


def get_github_pull_from_event(repo, event_type, data):
    if "pull_request" in data:
        return github.PullRequest.PullRequest(
            repo._requester, {}, data["pull_request"], completed=True
        )
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
            LOG.warning("check_suite/check_run attached on multiple pulls")

        for p in pulls:
            try:
                pull = repo.get_pull(p["number"])
            except github.UnknownObjectException:  # pragma: no cover
                continue

            if pull and not pull.merged:
                return pull


@app.task
def pull_request_opened():
    """Dumb method to record number of new PR for stats."""


@app.task
def pull_request_merged():
    """Dumb method to record number of closed PR for stats."""


@app.task
def pull_request_merged_by_mergify():
    """Dumb method to record number of closed PR for stats."""


def create_metrics(event_type, data):
    # prometheus_client is a mess with multiprocessing, so we generate tasks
    # that will be recorded by celery and exported with celery exporter
    if event_type == "pull_request" and data["action"] == "opened":
        pull_request_opened.apply_async()

    elif (event_type == "pull_request" and data["action"] == "closed" and
          data["pull_request"]["merged"]):

        pull_request_merged.apply_async()
        if data["pull_request"]["merged_by"]["login"] in ["mergify[bot]",
                                                          "mergify-test[bot]"]:
            pull_request_merged_by_mergify.apply_async()


def check_configuration_changes(event_pull):
    if event_pull.base.repo.default_branch == event_pull.base.ref:
        ref = None
        for f in event_pull.get_files():
            if f.filename == ".mergify.yml":
                ref = f.contents_url.split("?ref=")[1]

        if ref is not None:
            try:
                rules.get_mergify_config(event_pull.base.repo, ref=ref)
            except rules.InvalidRules as e:  # pragma: no cover
                # Not configured, post status check with the error message
                # TODO(sileht): we can annotate the .mergify.yml file in Github
                # UI with that API
                check_api.set_check_run(
                    event_pull, "Mergify — future config checker", "completed",
                    "failure", output={
                        "title": "The new Mergify configuration is invalid",
                        "summary": str(e)
                    })
            else:
                check_api.set_check_run(
                    event_pull, "Mergify — future config checker", "completed",
                    "success", output={
                        "title": "The new Mergify configuration is valid",
                        "summary": "No action required",
                    })

            check_api.set_check_run(
                event_pull, "Mergify — disabled due to configuration change",
                "completed", "success", output={
                    "title": "Mergify configuration has been modified",
                    "summary": "The pull request needs to be merged manually",
                })

            return True
    return False


@app.task
def run(event_type, data):
    """Everything starts here."""
    installation_id = data["installation"]["id"]
    installation_token = utils.get_installation_token(installation_id)
    if not installation_token:
        return

    g = github.Github(installation_token,
                      base_url="https://api.%s" % config.GITHUB_DOMAIN)
    try:
        if config.LOG_RATELIMIT:  # pragma: no cover
            rate = g.get_rate_limit().rate
            LOG.info("ratelimit: %s/%s, reset at %s",
                     rate.remaining, rate.limit, rate.reset,
                     repository=data["repository"]["name"])

        repo = g.get_repo(data["repository"]["owner"]["login"] + "/" +
                          data["repository"]["name"])

        event_pull = get_github_pull_from_event(repo, event_type, data)

        if not event_pull:  # pragma: no cover
            LOG.info("No pull request found in the event %s, "
                     "ignoring", event_type)
            return

        LOG.info("Pull request found in the event %s", event_type,
                 repo=repo.full_name,
                 pull_request=event_pull)

        subscription = sub_utils.get_subscription(utils.get_redis_for_cache(),
                                                  installation_id)

        if repo.private and not subscription["subscription_active"]:
            check_api.set_check_run(
                event_pull, "Summary",
                "completed", "failure", output={
                    "title": "Mergify is disabled",
                    "summary": subscription["subscription_reason"],
                })
            return

        if ("base" not in event_pull.raw_data or
                "repo" not in event_pull.raw_data["base"] or
                len(list(event_pull.raw_data["base"]["repo"].keys())) < 70):
            LOG.warning("the pull request payload looks suspicious",
                        event_type=event_type,
                        data=data,
                        pull_request=event_pull.raw_data,
                        repo=repo.fullname)

        if (event_type == "status" and
                event_pull.head.sha != data["sha"]):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of an old commit)",
                     repo=repo.full_name,
                     pull_request=event_pull)
            return

        elif (event_type in ["status", "check_suite", "check_run"] and
              event_pull.merged):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)",
                     repo=repo.full_name,
                     pull_request=event_pull)
            return
        elif (event_type in ["check_suite", "check_run"] and
              event_pull.head.sha != data[event_type]["head_sha"]
              ):  # pragma: no cover
            LOG.info("No need to proceed queue (got %s of an old "
                     "commit)", event_type,
                     repo=repo.full_name,
                     pull_request=event_pull)
            return

        if check_configuration_changes(event_pull):
            LOG.info("Configuration changed, ignoring",
                     repo=repo.full_name,
                     pull_request=event_pull)
            return

        # BRANCH CONFIGURATION CHECKING
        try:
            mergify_config = rules.get_mergify_config(repo)
        except rules.NoRules:  # pragma: no cover
            LOG.info("No need to proceed queue (.mergify.yml is missing)",
                     repo=repo.full_name,
                     pull_request=event_pull)
            return
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                check_api.set_check_run(
                    event_pull, "Summary", "completed",
                    "failure", output={
                        "title": "The Mergify configuration is invalid",
                        "summary": str(e)
                    })
            return

        create_metrics(event_type, data)

        v2.handle.s(
            installation_id,
            mergify_config["pull_request_rules"].as_dict(),
            event_type, data, event_pull.raw_data
        ).apply_async()
    except github.BadCredentialsException:  # pragma: no cover
        LOG.error("token for install %d is no longuer valid (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
