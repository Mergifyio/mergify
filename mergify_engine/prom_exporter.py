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

REPOSITORY_PER_INSTALLATION = prometheus_client.Gauge(
    "repositories_per_installation",
    "number of repositories per installation",
    ["subscribed", "type", "account", "private"])

USERS_PER_INSTALLATION = prometheus_client.Gauge(
    "users_per_installation", "number of users per installation",
    ["subscribed", "type", "account"])


def collect_metrics():
    redis = utils.get_redis_for_cache()
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    todo = []

    LOG.info("Get installations")
    for installation in utils.get_installations(integration):
        _id = installation["id"]
        LOG.info("Get subscription",
                 install=installation["account"]["login"])

        labels = {
            "subscribed": utils.get_subscription(redis, _id)["subscribed"],
            "type": installation["target_type"],
        }
        todo.append((INSTALLATIONS.labels(**labels).inc, tuple()))

        labels["account"] = installation["account"]["login"]

        token = integration.get_access_token(_id).token
        g = github.Github(token)

        if installation["target_type"] == "Organization":
            LOG.info("Get members",
                     install=installation["account"]["login"])
            org = g.get_organization(installation["account"]["login"])
            c = USERS_PER_INSTALLATION.labels(**labels)
            value = len(list(org.get_members()))
            todo.append((c.set, (value,)))

        LOG.info("Get repos",
                 install=installation["account"]["login"])

        repositories = sorted(g.get_installation(_id).get_repos(),
                              key=operator.attrgetter("private"))
        for private, repos in itertools.groupby(
                repositories, key=operator.attrgetter("private")):
            labels["private"] = private
            c = REPOSITORY_PER_INSTALLATION.labels(**labels)
            value = len(list(repos))
            todo.append((c.set, (value, )))

    # NOTE(sileht): Prometheus can scrape data during our loop. So make it fast
    # to ensure we always have the good values.
    # Also we can't known which labels we should delete from the Gauge,
    # that's why we delete all of them to re-add them.
    # And prometheus_client doesn't provide API to that, so we just
    # override _metrics
    INSTALLATIONS._metrics = {}
    REPOSITORY_PER_INSTALLATION._metrics = {}
    USERS_PER_INSTALLATION._metrics = {}
    for method, args in todo:
        method(*args)


def main():  # pragma: no cover
    utils.prepare_service()
    LOG.info("Starting")
    prometheus_client.start_http_server(8889)
    LOG.info("Started")

    while True:
        collect_metrics()
        # Only generate metrics once per hour
        time.sleep(60 * 60)
