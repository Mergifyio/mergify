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
    ["subscribed", "type", "account"])

USERS_PER_INSTALLATION = prometheus_client.Gauge(
    "users_per_installation", "number of users per installation",
    ["subscribed", "type", "account"])


def main():  # pragma: no cover
    utils.prepare_service()
    LOG.info("Starting")
    prometheus_client.start_http_server(8889)
    LOG.info("Started")

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    while True:
        r = utils.get_redis_for_cache()

        INSTALLATIONS._metrics = {}

        LOG.info("Get installations")
        for installation in utils.get_installations(integration):
            _id = installation["id"]
            LOG.info("Get subscription",
                     install=installation["account"]["login"])

            labels = {
                "subscribed": utils.get_subscription(r, _id)["subscribed"],
                "type": installation["target_type"],
            }
            INSTALLATIONS.labels(**labels).inc()

            labels["account"] = installation["account"]["login"]

            token = integration.get_access_token(_id).token
            g = github.Github(token)

            if installation["target_type"] == "Organization":
                LOG.info("Get members",
                         install=installation["account"]["login"])
                org = g.get_organization(installation["account"]["login"])
                c = USERS_PER_INSTALLATION.labels(**labels)
                c.set(len(list(org.get_members())))

            LOG.info("Get repos",
                     install=installation["account"]["login"])

            repositories = sorted(g.get_installation(_id).get_repos(),
                                  key=operator.attrgetter("private"))
            for private, repos in itertools.groupby(
                    repositories, key=operator.attrgetter("private")):
                labels["private"] = private
                c = REPOSITORY_PER_INSTALLATION.labels(**labels)
                c.set(len(repos))

        # Only generate metrics once per hour
        time.sleep(60 * 60)
