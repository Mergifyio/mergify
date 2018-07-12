# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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


import logging

import github
import raven
from raven.transport.http import HTTPTransport
import rq
from rq.contrib.sentry import register_sentry
import rq.handlers
import rq.worker

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import gh_pr
from mergify_engine import initial_configuration
from mergify_engine import utils

LOG = logging.getLogger(__name__)


global QUEUES
QUEUES = None


def event_handler(event_type, subscription, data):
    """Everything start here"""
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_token = integration.get_access_token(
        data["installation"]["id"]).token
    g = github.Github(installation_token)
    try:
        user = g.get_user(data["repository"]["owner"]["login"])
        repo = user.get_repo(data["repository"]["name"])

        engine.MergifyEngine(g, data["installation"]["id"],
                             installation_token,
                             subscription,
                             user, repo).handle(event_type, data)
    except github.GithubException.BadCredentialsException:
        LOG.error("token for install %d is no longuer valid (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])
    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])


def installation_handler(installation_id, repositories):
    """Create the initial configuration on an repository"""

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_token = integration.get_access_token(
        installation_id).token
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
            initial_configuration.create_pull_request_if_needed(
                installation_token, repository)

    except github.RateLimitExceededException:  # pragma: no cover
        LOG.error("rate limit reached for install %d",
                  installation_id)


def retry_handler(job, exc_type, exc_value, traceback):
    global QUEUES

    if exc_type != exceptions.RetryJob:
        return True

    job.meta.setdefault('failures', 0)
    job.meta['failures'] += 1

    # Too many failures
    if job.meta['failures'] >= exc_value.retries:
        LOG.warn('job %s: failed too many times times - moving to failed queue'
                 % job.id)
        job.save()
        return True

    # Requeue job and stop it from being moved into the failed queue
    LOG.warn('job %s: failed %d times - retrying' % (job.id,
                                                     job.meta['failures']))

    for queue in QUEUES:
        if queue.name == job.origin:
            queue.enqueue_job(job, at_front=True)
            return False

    # Can't find queue, which should basically never happen as we only work
    # jobs that match the given queue names and queues are transient in rq.
    LOG.warn('job %s: cannot find queue %s - moving to failed queue' %
             (job.id, job.origin))
    return True


def main():  # pragma: no cover
    global QUEUES

    utils.setup_logging()
    config.log()
    gh_pr.monkeypatch_github()
    r = utils.get_redis_for_rq()
    if config.FLUSH_REDIS_ON_STARTUP:
        r.flushall()
    with rq.Connection(r):
        QUEUES = [rq.Queue('default')]
        worker = rq.Worker(QUEUES)
        worker.push_exc_handler(retry_handler)
        if config.SENTRY_URL:
            client = raven.Client(config.SENTRY_URL,
                                  transport=HTTPTransport)
            register_sentry(client, worker)
        worker.work()


if __name__ == '__main__':  # pragma: no cover
    main()
