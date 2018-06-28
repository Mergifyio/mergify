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
import rq
import rq.worker

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import gh_pr
from mergify_engine import initial_configuration
from mergify_engine import utils

LOG = logging.getLogger(__name__)


def real_event_handler(event_type, subscription, data):
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
    except github.RateLimitExceededException:
        LOG.error("rate limit reached for install %d (%s)",
                  data["installation"]["id"],
                  data["repository"]["full_name"])


def event_handler(event_type, subscription, data):
    # NOTE(sileht): This is just here for easy mocking purpose
    # rq pickles the method, so it must be as simple as possible
    return real_event_handler(event_type, subscription, data)


def private_installation_handler(installation_id):
    """Create the initial configuration on all private repositories"""

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_token = integration.get_access_token(
        installation_id).token
    g = github.Github(installation_token)
    try:
        installation = g.get_installation(installation_id)
        for repo in installation.get_repos():
            if repo.private:
                initial_configuration.create_pull_request_if_needed(
                    installation_token, repo)
    except github.RateLimitExceededException:
        LOG.error("rate limit reached for install %d", installation_id)


def installation_handler(installation, repository):
    """Create the initial configuration on an repository"""

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installation_token = integration.get_access_token(
        installation["id"]).token
    g = github.Github(installation_token)
    try:
        user = g.get_user(installation["account"]["login"])
        repo = user.get_repo(repository["name"])
        initial_configuration.create_pull_request_if_needed(
            installation_token, repo)
    except github.RateLimitExceededException:
        LOG.error("rate limit reached for install %d (%s)",
                  installation["id"],
                  repository["full_name"])


def main():
    utils.setup_logging()
    config.log()
    gh_pr.monkeypatch_github()
    r = utils.get_redis_for_rq()
    if config.FLUSH_REDIS_ON_STARTUP:
        r.flushall()
    with rq.Connection(r):
        worker = rq.Worker(['default'])
        worker.work()


if __name__ == '__main__':
    main()
