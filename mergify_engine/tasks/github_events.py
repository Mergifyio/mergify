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

import itertools

import daiquiri

import github

import uhashring

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import initial_configuration
from mergify_engine import stats
from mergify_engine import utils
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


@app.task
def job_refresh(owner, repo, refresh_ref):
    LOG.info("%s/%s/%s: refreshing", owner, repo, refresh_ref)

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_id = utils.get_installation_id(integration, owner)
    if not installation_id:  # pragma: no cover
        LOG.warning("%s/%s/%s: mergify not installed",
                    owner, repo, refresh_ref)
        return

    token = integration.get_access_token(installation_id).token
    g = github.Github(token)
    r = g.get_repo("%s/%s" % (owner, repo))
    try:
        r.get_contents(".mergify.yml")
    except github.GithubException as e:  # pragma: no cover
        if e.status == 404:
            LOG.warning("%s/%s/%s: mergify not configured",
                        owner, repo, refresh_ref)
            return
        else:
            raise

    if refresh_ref == "full" or refresh_ref.startswith("branch/"):
        if refresh_ref.startswith("branch/"):
            branch = refresh_ref[7:]
            pulls = r.get_pulls(base=branch)
        else:
            branch = '*'
            pulls = r.get_pulls()
        key = "queues~%s~%s~%s~%s~%s" % (installation_id, owner.lower(),
                                         repo.lower(), r.private, branch)
        utils.get_redis_for_cache().delete(key)
    else:
        try:
            pull_number = int(refresh_ref[5:])
        except ValueError:  # pragma: no cover
            LOG.info("%s/%s/%s: Invalid PR ref", owner, repo, refresh_ref)
            return
        pulls = [r.get_pull(pull_number)]

    subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                          installation_id)

    if r.archived:  # pragma: no cover
        LOG.warning("%s/%s/%s: repository archived",
                    owner, repo, refresh_ref)
        return

    if not subscription["token"]:  # pragma: no cover
        LOG.warning("%s/%s/%s: not public or subscribed",
                    owner, repo, refresh_ref)
        return

    if r.private and not subscription["subscribed"]:  # pragma: no cover
        LOG.warning("%s/%s/%s: mergify not installed",
                    owner, repo, refresh_ref)
        return

    for p in pulls:
        # Mimic the github event format
        data = {
            'repository': r.raw_data,
            'installation': {'id': installation_id},
            'pull_request': p.raw_data,
        }
        job_run_engine('refresh', data, subscription)


@app.task
def job_refresh_all():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    counts = [0, 0, 0]
    for install in utils.get_installations(integration):
        counts[0] += 1
        token = integration.get_access_token(install["id"]).token
        g = github.Github(token)
        i = g.get_installation(install["id"])

        subscription = utils.get_subscription(utils.get_redis_for_cache(),
                                              install["id"])
        if not subscription["token"]:  # pragma: no cover
            continue

        for r in i.get_repos():
            if r.archived:  # pragma: no cover
                continue
            if r.private and not subscription["subscribed"]:
                continue
            try:
                r.get_contents(".mergify.yml")
            except github.GithubException as e:  # pragma: no cover
                if e.status == 404:
                    continue
                else:
                    raise

            counts[1] += 1
            for p in list(r.get_pulls()):
                # Mimic the github event format
                data = {
                    'repository': r.raw_data,
                    'installation': {'id': install["id"]},
                    'pull_request': p.raw_data,
                }
                job_run_engine('refresh', data, subscription)

    LOG.info("Refreshing %s installations, %s repositories, "
             "%s branches", *counts)


@app.task
def job_filter_and_dispatch(event_type, event_id, data):
    subscription = utils.get_subscription(
        utils.get_redis_for_cache(), data["installation"]["id"])

    if not subscription["token"]:
        msg_action = "ignored (no token)"

    elif event_type == "installation" and data["action"] == "created":
        stats.INSTALLATIION_ADDED.inc()
        for repository in data["repositories"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue

            job_installations.delay(data["installation"]["id"],
                                    [repository])
        msg_action = "pushed to backend"

    elif event_type == "installation" and data["action"] == "deleted":
        stats.INSTALLATIION_REMOVED.inc()

        # TODO(sileht): move out this engine V1 related code
        key = "queues~%s~*~*~*~*" % data["installation"]["id"]
        utils.get_redis_for_cache().delete(key)
        msg_action = "handled, cache cleaned"

    elif (event_type == "installation_repositories" and
          data["action"] == "added"):
        for repository in data["repositories_added"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue

            job_installations.delay(data["installation"]["id"], [repository])

        msg_action = "pushed to backend"

    elif (event_type == "installation_repositories" and
          data["action"] == "removed"):
        for repository in data["repositories_removed"]:
            if repository["private"] and not subscription["subscribed"]:  # noqa pragma: no cover
                continue

            # TODO(sileht): move out this engine V1 related code
            key = "queues~%s~%s~%s~*~*" % (
                data["installation"]["id"],
                data["installation"]["account"]["login"].lower(),
                repository["name"].lower()
            )
            utils.get_redis_for_cache().delete(key)

        msg_action = "handled, cache cleaned"

    elif event_type in ["installation", "installation_repositories"]:
        msg_action = "ignored (action %s)" % data["action"]

    elif event_type in ["pull_request", "pull_request_review", "status"]:

        if data["repository"]["archived"]:  # pragma: no cover
            msg_action = "ignored (repository archived)"

        elif (data["repository"]["private"] and not
                subscription["subscribed"]):
            msg_action = "ignored (not public or subscribe)"

        elif event_type == "status" and data["state"] == "pending":
            msg_action = "ignored (state pending)"

        elif event_type == "status" and data["context"] == "mergify/pr":
            msg_action = "ignored (mergify status)"

        elif (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize",
                "labeled", "unlabeled"]):
            msg_action = "ignored (action %s)" % data["action"]

        else:
            job_run_engine(event_type, data, subscription)
            msg_action = "pushed to backend"

    else:
        msg_action = "ignored (unexpected event_type)"

    if "repository" in data:
        repo_name = data["repository"]["full_name"]
    else:
        repo_name = data["installation"]["account"]["login"]

    LOG.info('event %s', msg_action,
             event_type=event_type,
             event_id=event_id,
             install_id=data["installation"]["id"],
             repository=repo_name,
             subscribed=subscription["subscribed"])


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


def job_run_engine(event_type, data, subscription):
    # NOTE(sileht): Select the worker to use, only useful for engine v1. This
    # work in coordination with app.conf.worker_direct = True that creates the
    # dedicated queue on exchange c.dq2
    ring = RINGS_PER_SUBSCRIPTION[subscription["subscribed"]]
    routing_key = ring.get_node(data["repository"]["full_name"])
    LOG.info("Sending repo %s to %s", data["repository"]["full_name"],
             routing_key)
    _job_run_engine.s(event_type, data, subscription).apply_async(
        exchange='C.dq2', routing_key=routing_key)


@app.task
def _job_run_engine(event_type, data, subscription):
    """Everything starts here."""
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    try:
        installation_token = integration.get_access_token(
            data["installation"]["id"]).token
    except github.UnknownObjectException:
        LOG.error("token for install %d does not exists anymore (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
        return

    g = github.Github(installation_token)
    try:
        user = g.get_user(data["repository"]["owner"]["login"])
        repo = user.get_repo(data["repository"]["name"])

        # TODO(sileht): We currently launch only engine v1 here.
        # We must extract common from engine v1:
        # * the code that extract pull request object from the event
        #   or via github search when we only have sha
        # * the code that extract mergify configuration from the repository
        engine.MergifyEngine(g, data["installation"]["id"],
                             installation_token,
                             subscription,
                             user, repo).handle(event_type, data)
    except github.BadCredentialsException:  # pragma: no cover
        LOG.error("token for install %d is no longuer valid (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])


@app.task
def job_refresh_private_installations(installation_id):
    # New subscription, create initial configuration for private repo
    # public repository have already been done during the installation
    # event.
    r = utils.get_redis_for_cache()
    r.delete("subscription-cache-%s" % installation_id)

    subscription = utils.get_subscription(r, installation_id)
    if subscription["token"] and subscription["subscribed"]:
        job_installations.delay(installation_id, "private")


@app.task
def job_installations(installation_id, repositories):
    """Create the initial configuration on an repository."""
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    try:
        installation_token = integration.get_access_token(
            installation_id).token
    except github.UnknownObjectException:  # pragma: no cover
        LOG.error("token for install %d does not exists anymore",
                  installation_id)
        return

    g = github.Github(installation_token)
    try:
        if isinstance(repositories, str):
            installation = g.get_installation(installation_id)
            if repositories == "private":
                repositories = [repo for repo in installation.get_repos()
                                if repo.private]
            elif repositories == "all":
                repositories = [repo for repo in installation.get_repos()]
            else:
                raise RuntimeError("Unexpected 'repositories' format: %s",
                                   type(repositories))
        elif isinstance(repositories, list):
            # Some events return incomplete repository structure (like
            # installation event). Complete them in this case
            new_repos = []
            for repository in repositories:
                user = g.get_user(repository["full_name"].split("/")[0])
                repo = user.get_repo(repository["name"])
                new_repos.append(repo)
            repositories = new_repos
        else:  # pragma: no cover
            raise RuntimeError("Unexpected 'repositories' format: %s",
                               type(repositories))

        for repository in repositories:
            # NOTE(sileht): the installations event doesn't have this
            # attribute, so we keep it here.
            if repository.archived:  # pragma: no cover
                continue
            initial_configuration.create_pull_request_if_needed(
                installation_token, repository)

    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d", installation_id)
