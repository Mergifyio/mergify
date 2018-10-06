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
from mergify_engine.tasks.engine import v1
from mergify_engine.tasks.engine import v2
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def get_github_pull_from_sha(g, repo, installation_id, sha):

    # TODO(sileht): Replace this optimisation when we drop engine v1
    pull = v1.Caching(repository=repo,
                      installation_id=installation_id
                      ).get_pr_for_sha(sha)
    if pull:
        return pull

    issues = list(g.search_issues("repo:%s is:pr is:open %s" %
                                  (repo.full_name, sha)))
    if not issues:
        return
    if len(issues) > 1:  # pragma: no cover
        # NOTE(sileht): It's that technically possible, but really ?
        LOG.warning("sha attached to multiple pull requests", sha=sha)
    for i in issues:
        try:
            pull = repo.get_pull(i.number)
        except github.GithubException as e:  # pragma: no cover
            if e.status != 404:
                raise
        if pull and not pull.merged:
            return pull


def get_github_pull_from_event(g, repo, installation_id,
                               event_type, data):
    if "pull_request" in data:
        return github.PullRequest.PullRequest(
            repo._requester, {}, data["pull_request"], completed=True
        )
    elif event_type == "status":
        return get_github_pull_from_sha(g, repo, installation_id, data["sha"])

    elif event_type in ["check_suite", "check_run"]:
        if event_type == "check_run":
            pulls = data["check_run"]["check_suite"]["pull_requests"]
            sha = data["check_run"]["head_sha"]
        else:
            pulls = data["check_suite"]["pull_requests"]
            sha = data["check_suite"]["head_sha"]
        if not pulls:
            return get_github_pull_from_sha(g, repo, installation_id, sha)
        if len(pulls) > 1:  # pragma: no cover
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning("check_suite/check_run attached on multiple pulls")

        for p in pulls:

            pull = v1.Caching(repository=repo,
                              installation_id=installation_id
                              ).get_pr_for_pull_number(
                                  p["base"]["ref"],
                                  p["number"])
            if not pull:
                try:
                    pull = repo.get_pull(p["number"])
                except github.UnknownObjectException:  # pragma: no cover
                    pass

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


def check_configuration_changes(event_type, data, event_pull):
    if (event_type == "pull_request" and
        data["action"] in ["opened", "synchronize"] and
            event_pull.base.repo.default_branch == event_pull.base.ref):
        ref = None
        for f in event_pull.get_files():
            if f.filename == ".mergify.yml":
                ref = f.contents_url.split("?ref=")[1]

        if ref is not None:
            try:
                mergify_config = rules.get_mergify_config(
                    event_pull.base.repo, ref=ref)
                if "rules" in mergify_config:
                    rules.get_branch_rule(mergify_config['rules'],
                                          event_pull.base.ref)
            except rules.InvalidRules as e:  # pragma: no cover
                # Not configured, post status check with the error message
                # TODO(sileht): we can annotate the .mergify.yml file in Github
                # UI with that API
                check_api.set_check_run(
                    event_pull, "future-config-checker", "completed",
                    "failure", output={
                        "title": "The new Mergify configuration is invalid",
                        "summary": str(e)
                    })
            else:
                check_api.set_check_run(
                    event_pull, "future-config-checker", "completed",
                    "success", output={
                        "title": "The new Mergify configuration is valid",
                        "summary": "No action required",
                    })


@app.task
def run(event_type, data, subscription):
    """Everything starts here."""
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_id = data["installation"]["id"]
    try:
        installation_token = integration.get_access_token(
            installation_id).token
    except github.UnknownObjectException:  # pragma: no cover
        LOG.error("token for install %d does not exists anymore (%s)",
                  installation_id, data["repository"]["full_name"])
        return

    g = github.Github(installation_token)
    try:
        if config.LOG_RATELIMIT:  # pragma: no cover
            rate = g.get_rate_limit().rate
            LOG.info("ratelimit: %s/%s, reset at %s",
                     rate.remaining, rate.limit, rate.reset,
                     repository=data["repository"]["name"])

        repo = g.get_repo(data["repository"]["owner"]["login"] + "/" +
                          data["repository"]["name"])

        # NOTE(sileht): Workaround for when we receive check_suite completed
        # without conclusion
        if (event_type == "check_suite" and data["action"] == "completed" and
                not data["check_suite"]["conclusion"]):  # pragma: no cover
            data = check_api.workaround_for_unfinished_check_suite(
                repo, data)

        event_pull = get_github_pull_from_event(g, repo, installation_id,
                                                event_type, data)

        if not event_pull:  # pragma: no cover
            LOG.info("No pull request found in the event %s, "
                     "ignoring", event_type)
            return

        LOG.info("Pull request found in the event %s", event_type,
                 pull_request=event_pull)

        if (event_type == "status" and
                event_pull.head.sha != data["sha"]):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        elif (event_type in ["status", "check_suite", "check_run"] and
              event_pull.merged):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)")
            return
        elif (event_type in ["check_suite", "check_run"] and
              event_pull.head.sha != data[event_type]["head_sha"]
              ):  # pragma: no cover
            LOG.info("No need to proceed queue (got %s of an old "
                     "commit)", event_type)
            return

        check_configuration_changes(event_type, data, event_pull)

        # BRANCH CONFIGURATION CHECKING
        try:
            mergify_config = rules.get_mergify_config(repo)
        except rules.NoRules as e:  # pragma: no cover
            LOG.info("No need to proceed queue (.mergify.yml is missing)")
            return
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                check_api.set_check_run(
                    event_pull, "current-config-checker", "completed",
                    "failure", output={
                        "title": "The Mergify configuration is invalid",
                        "summary": str(e)
                    })
            return

        create_metrics(event_type, data)

        # NOTE(sileht): At some point we may need to reget the
        # installation_token within each next tasks, in case we reach the
        # expiration
        if "rules" in mergify_config:
            v1.handle.s(installation_id, installation_token, subscription,
                        mergify_config["rules"], event_type, data,
                        event_pull.raw_data).apply_async()

        elif "pull_request_rules" in mergify_config:
            v2.handle.s(
                installation_id, installation_token, subscription,
                mergify_config["pull_request_rules"].as_dict(),
                event_type, data, event_pull.raw_data
            ).apply_async()

        else:  # pragma: no cover
            raise RuntimeError("Unexpected configuration version")

    except github.BadCredentialsException:  # pragma: no cover
        LOG.error("token for install %d is no longuer valid (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
