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


import github

import prometheus_client

from mergify_engine import config
from mergify_engine import utils

INSTALLATIION_ADDED = prometheus_client.Counter(
    "installation_added", "Number of installation added")

INSTALLATIION_REMOVED = prometheus_client.Counter(
    "installation_removed", "Number of installation removed")

PULL_REQUESTS = prometheus_client.Counter(
    "pull_requests", "Total number of pull request handled")


def get_installations():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    return utils.get_installations(integration)


def get_active_installations():
    installs = set()
    for key in utils.get_redis_for_cache().keys("queues~*"):
        installs.add(key.split("~")[2])
    return installs


def get_active_slugs():
    active_slugs = set()
    for key in utils.get_redis_for_cache().keys("queues~*"):
        _, _, owner, repo, private, branch = key.split("~")
        active_slugs.add("%s/%s" % (owner, repo))
    return active_slugs


def get_active_pull_requests():
    redis = utils.get_redis_for_cache()
    pulls = 0
    for key in redis.keys("queues~*"):
        pulls += redis.hlen(key)
    return pulls

# FIXME(sileht): since rq spawn a new process for each event this gauge system
# create a timeserie per pid. Disable them for now.
#
# INSTALLATIONS = prometheus_client.Gauge(
#     "installations", "number of installations")
# INSTALLATIONS.set_function(lambda: len(get_installations()))
#
# ACTIVE_INSTALLS = prometheus_client.Gauge(
#     "active_installations", "number of active installations")
# ACTIVE_INSTALLS.set_function(lambda: len(get_active_installations()))
#
# ACTIVE_SLUGS = prometheus_client.Gauge(
#     "active_slugs", "number of active slugs")
# ACTIVE_SLUGS.set_function(lambda: len(get_active_slugs()))
#
# ACTIVE_PULL_REQUESTS = prometheus_client.Gauge(
#     "pull_requests_active", "Number of active pull request")
# ACTIVE_PULL_REQUESTS.set_function(get_active_pull_requests)


def main():  # pragma: no cover
    installs = get_installations()
    r = utils.get_redis_for_cache()
    subscribed = 0
    for i in installs:
        if utils.get_subscription(r, i["id"])["subscribed"]:
            subscribed += 1
    print("installations: %d" % len(installs))
    print("active_installations: %d" % len(get_active_installations()))
    print("subscriptions: %d" % subscribed)

    active_slugs = get_active_slugs()
    print("repositories with pending PRs: %d" % len(active_slugs))
    print(" * %s" % "\n * ".join(sorted(active_slugs)))
