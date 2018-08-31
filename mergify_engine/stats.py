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


import github

import prometheus_client

from mergify_engine import config
from mergify_engine import utils

GITHUB_EVENTS = prometheus_client.Counter(
    "github_events", "Number of Github events handled")

INSTALLATIION_ADDED = prometheus_client.Counter(
    "installation_added", "Number of installation added")

INSTALLATIION_REMOVED = prometheus_client.Counter(
    "installation_removed", "Number of installation removed")

PULL_REQUESTS = prometheus_client.Counter(
    "pull_requests", "Number of pull request handled")

INSTALLATIONS_TIME = prometheus_client.Summary(
    'job_installations_processing_seconds',
    'Time spent processing job_installations')

JOB_EVENTS_TIME = prometheus_client.Summary(
    'job_events_processing_seconds',
    'Time spent processing job_events')

JOB_FILTER_AND_DISPATCH_TIME = prometheus_client.Summary(
    'job_filter_and_dispatch_processing_seconds',
    'Time spent processing job_filter_and_dispatch')


def get_installations():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    return utils.get_installations(integration)


INSTALLATIONS = prometheus_client.Gauge(
    "installations", "Number of installations")

INSTALLATIONS.set_function(lambda: len(get_installations()))


def main():  # pragma: no cover
    installs = get_installations()
    r = utils.get_redis_for_cache()
    subscribed = 0
    for i in installs:
        if utils.get_subscription(r, i["id"])["subscribed"]:
            subscribed += 1
    print("installations: %d" % len(installs))
    print("subscriptions: %d" % subscribed)

    active_slugs = set()
    for key in utils.get_redis_for_cache().keys("queues~*"):
        _, _, owner, repo, private, branch = key.split("~")
        active_slugs.add("%s/%s" % (owner, repo))

    print("repositories with pending PRs: %d" % len(active_slugs))
    print(" * %s" % "\n * ".join(sorted(active_slugs)))
