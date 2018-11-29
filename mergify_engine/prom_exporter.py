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

import collections
import itertools
import operator
import time

import daiquiri

import github

import prometheus_client

from mergify_engine import config
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


INSTALLATIONS = prometheus_client.Gauge(
    "installations", "number of installations",
    ["subscribed", "type"])

REPOSITORIES_PER_INSTALLATION = prometheus_client.Gauge(
    "repositories_per_installation",
    "number of repositories per installation",
    ["subscribed", "type", "account", "private", "configured"])

USERS_PER_INSTALLATION = prometheus_client.Gauge(
    "users_per_installation", "number of users per installation",
    ["subscribed", "type", "account"])


def set_gauge(metric, labels, value):
    metric.labels(*labels).set(value)


def set_gauges(metric, data):
    metric._metrics = {}
    list(map(lambda d: set_gauge(metric, *d), data.items()))


def collect_metrics():
    redis = utils.get_redis_for_cache()
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    installations = collections.defaultdict(int)
    repositories_per_installation = collections.defaultdict(int)
    users_per_installation = collections.defaultdict(int)

    LOG.info("Get installations")
    for installation in utils.get_installations(integration):
        _id = installation["id"]
        target_type = installation["target_type"]
        account = installation["account"]["login"]

        LOG.info("Get subscription", account=account)
        subscribed = utils.get_subscription(
            redis, _id)["subscribed"]

        installations[(subscribed, target_type)] += 1

        token = integration.get_access_token(_id).token
        g = github.Github(token, base_url="https://api.%s" %
                          config.GITHUB_DOMAIN)

        if installation["target_type"] == "Organization":  # pragma: no cover
            LOG.info("Get members",
                     install=installation["account"]["login"])
            org = g.get_organization(installation["account"]["login"])
            value = len(list(org.get_members()))

            users_per_installation[
                (subscribed, target_type, account)] = value
        else:
            users_per_installation[
                (subscribed, target_type, account)] = 1

        LOG.info("Get repos", account=account)

        repositories = sorted(g.get_installation(_id).get_repos(),
                              key=operator.attrgetter("private"))
        for private, repos in itertools.groupby(
                repositories, key=operator.attrgetter("private")):

            configured_repos = 0
            unconfigured_repos = 0
            for repo in repos:
                try:
                    repo.get_contents(".mergify.yml")
                    configured_repos += 1
                except github.GithubException as e:
                    if e.status >= 500:  # pragma: no cover
                        raise
                    unconfigured_repos += 1

            repositories_per_installation[
                (subscribed, target_type, account, private, True)
            ] = configured_repos
            repositories_per_installation[
                (subscribed, target_type, account, private, False)
            ] = unconfigured_repos

    # NOTE(sileht): Prometheus can scrape data during our loop. So make it fast
    # to ensure we always have the good values.
    # Also we can't known which labels we should delete from the Gauge,
    # that's why we delete all of them to re-add them.
    # And prometheus_client doesn't provide API to that, so we just
    # override _metrics
    set_gauges(INSTALLATIONS, installations)
    set_gauges(USERS_PER_INSTALLATION, users_per_installation)
    set_gauges(REPOSITORIES_PER_INSTALLATION, repositories_per_installation)


def main():  # pragma: no cover
    utils.prepare_service()
    LOG.info("Starting")
    prometheus_client.start_http_server(8889)
    LOG.info("Started")

    while True:
        try:
            collect_metrics()
        except Exception:
            LOG.error("fail to gather metrics", exc_info=True)
        # Only generate metrics once per hour
        time.sleep(60 * 60)
