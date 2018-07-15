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

from mergify_engine import config
from mergify_engine import utils


def main():  # pragma: no cover
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    installs = utils.get_installations(integration)
    print("installations: %d" % len(installs))

    active_slugs = set()
    for key in utils.get_redis_for_cache().keys("queues~*"):
        _, _, owner, repo, private, branch = key.split("~")
        active_slugs.add("%s/%s" % (owner, repo))

    print("repositories with pending PRs: %d" % len(active_slugs))
    print(" * %s" % "\n * ".join(sorted(active_slugs)))
