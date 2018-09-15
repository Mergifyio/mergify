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

import itertools

import daiquiri

import github

import uhashring

from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine.tasks.engine import v1
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def get_ring(topology, kind):
    return uhashring.HashRing(
        nodes=list(itertools.chain.from_iterable(
            map(lambda x: "worker-%s-%003d@%s" % (kind, x, fqdn), range(w))
            for fqdn, w in sorted(topology.items())
        )))


RINGS_PER_SUBSCRIPTION = {
    True: get_ring(config.TOPOLOGY_SUBSCRIBED, "sub"),
    False: get_ring(config.TOPOLOGY_FREE, "free")
}


def run(event_type, data, subscription):
    # NOTE(sileht): Select the worker to use, only useful for engine v1. This
    # work in coordination with app.conf.worker_direct = True that creates the
    # dedicated queue on exchange c.dq2
    ring = RINGS_PER_SUBSCRIPTION[subscription["subscribed"]]
    routing_key = ring.get_node(data["repository"]["full_name"])
    LOG.info("Sending repo %s to %s", data["repository"]["full_name"],
             routing_key)
    _job_run.s(event_type, data, subscription).apply_async(
        exchange='C.dq2', routing_key=routing_key)


def get_github_pull_from_sha(g, user, repo, installation_id, sha):

    # TODO(sileht): Replace this optimisation when we drop engine v1
    pull = v1.Caching(user=user, repository=repo,
                      installation_id=installation_id
                      ).get_pr_for_sha(sha)
    if pull:
        return pull

    issues = list(g.search_issues("is:pr %s" % sha))
    if not issues:
        return
    if len(issues) > 1:
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


def get_github_pull_from_event(g, user, repo, installation_id,
                               event_type, data):
    if "pull_request" in data:
        return github.PullRequest.PullRequest(
            repo._requester, {},
            data["pull_request"], completed=True
        )
    elif event_type == "status":
        return get_github_pull_from_sha(g, user, repo, installation_id,
                                        data["sha"])

    elif event_type in ["check_suite", "check_run"]:
        if event_type == "check_run":
            pulls = data["check_run"]["check_suite"]["pull_requests"]
            sha = data["check_run"]["head_sha"]
        else:
            pulls = data["check_suite"]["pull_requests"]
            sha = data["check_suite"]["head_sha"]
        if not pulls:
            return get_github_pull_from_sha(g, user, repo,
                                            installation_id, sha)
        if len(pulls) > 1:  # pragma: no cover
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning("check_suite/check_run attached on multiple pulls")

        for p in pulls:

            pull = v1.Caching(user=user, repository=repo,
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


@app.task
def _job_run(event_type, data, subscription):
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

        user = g.get_user(data["repository"]["owner"]["login"])
        repo = user.get_repo(data["repository"]["name"])

        event_pull = get_github_pull_from_event(g, user, repo, installation_id,
                                                event_type, data)

        if not event_pull:  # pragma: no cover
            LOG.info("No pull request found in the event %s, "
                     "ignoring", event_type)
            return

        incoming_pull = mergify_pull.MergifyPull(event_pull, installation_id)
        incoming_branch = incoming_pull.g_pull.base.ref
        incoming_sha = incoming_pull.g_pull.head.sha

        LOG.info("Pull request found in the event %s", event_type,
                 pull_request=incoming_pull)

        if (event_type == "status" and
                incoming_sha != data["sha"]):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        elif (event_type in ["status", "check_suite", "check_run"]
              and incoming_pull.g_pull.merged):  # noqa pragma: no cover
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)")
            return
        elif (event_type in ["check_suite", "check_run"]
              and incoming_pull.g_pull.head.sha != data[event_type]["head_sha"]):  # noqa pragma: no cover
            LOG.info("No need to proceed queue (got %s of an old "
                     "commit)", event_type)
            return

        # CHECK IF THE CONFIGURATION IS GOING TO CHANGE
        if (event_type == "pull_request" and
            data["action"] in ["opened", "synchronize"] and
                repo.default_branch == incoming_branch):
            ref = None
            for f in incoming_pull.g_pull.get_files():
                if f.filename == ".mergify.yml":
                    ref = f.contents_url.split("?ref=")[1]

            if ref is not None:
                try:
                    mergify_config = rules.get_mergify_config(
                        repo, ref=ref)
                    rules.get_branch_rule(mergify_config['rules'],
                                          incoming_branch)
                except rules.InvalidRules as e:  # pragma: no cover
                    # Not configured, post status check with the error message
                    # FIXME()!!!!!!!!!!!
                    incoming_pull.post_check_status(
                        "failure", str(e), "future-config-checker")
                else:
                    incoming_pull.post_check_status(
                        "success", "The new configuration is valid",
                        "future-config-checker")

        # BRANCH CONFIGURATION CHECKING
        try:
            mergify_config = rules.get_mergify_config(repo)
        except rules.NoRules as e:
            LOG.info("No need to proceed queue (.mergify.yml is missing)")
            return
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                incoming_pull.post_check_status(
                    "failure", str(e))
            return

        create_metrics(event_type, data)

        if "rules" in mergify_config:
            v1.MergifyEngine(
                g, installation_id, installation_token,
                subscription, user, repo
            ).handle(mergify_config, event_type, incoming_pull, data)
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
