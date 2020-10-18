# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import asyncio
import operator

import yaml

from mergify_engine import config
from mergify_engine import context
from mergify_engine import subscription
from mergify_engine.tests.functional import base


class TestPostCheckAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    def test_checks_default(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "body need sentry ticket",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "#title>10",
                        "#title<50",
                        "#body<4096",
                        "#files<100",
                        "body~=(?m)^(Fixes|Related|Closes) (MERGIFY-ENGINE|MRGFY)-",
                        "-label=ignore-guideline",
                    ],
                    "actions": {"post_check": {}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        self.run_engine()
        p.update()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        sorted_checks = list(
            sorted(ctxt.pull_engine_check_runs, key=operator.itemgetter("name"))
        )
        assert len(sorted_checks) == 2
        check = sorted_checks[0]
        assert "failure" == check["conclusion"]
        assert "'body need sentry ticket' failed" == check["output"]["title"]

    def test_checks_custom(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "body need sentry ticket",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "#title>10",
                        "#title<50",
                        "#body<4096",
                        "#files<100",
                        "body~=(?m)^(Fixes|Related|Closes) (MERGIFY-ENGINE|MRGFY)-",
                        "-label=ignore-guideline",
                    ],
                    "actions": {
                        "post_check": {
                            "title": "Pull request #{{ number }} does{% if not check_succeed %} not{% endif %} follow our guideline",
                            "summary": """
Full markdown of my awesome pull request guideline:

* Mandatory stuff about title
* Need a ticket number
* Please explain what your trying to achieve

Rule list:

{{ check_conditions }}

""",
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        self.run_engine()
        p.update()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        sorted_checks = list(
            sorted(ctxt.pull_engine_check_runs, key=operator.itemgetter("name"))
        )
        assert len(sorted_checks) == 2
        check = sorted_checks[0]
        assert (
            f"Pull request #{p.number} does not follow our guideline"
            == check["output"]["title"]
        )
        assert "failure" == check["conclusion"]


class TestPostCheckActionNoSub(base.FunctionalTestBase):
    def test_checks_feature_disabled(self):
        self.subscription = subscription.Subscription(
            config.INSTALLATION_ID,
            self.SUBSCRIPTION_ACTIVE,
            "You're not nice",
            {"mergify-test1": config.ORG_ADMIN_GITHUB_APP_OAUTH_TOKEN},
            frozenset(
                getattr(subscription.Features, f)
                for f in subscription.Features.__members__
                if f is not subscription.Features.CUSTOM_CHECKS
            )
            if self.SUBSCRIPTION_ACTIVE
            else frozenset(),
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.subscription.save_subscription_to_cache())

        rules = {
            "pull_request_rules": [
                {
                    "name": "body need sentry ticket",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "#title>10",
                        "#title<50",
                        "#body<4096",
                        "#files<100",
                        "body~=(?m)^(Fixes|Related|Closes) (MERGIFY-ENGINE|MRGFY)-",
                        "-label=ignore-guideline",
                    ],
                    "actions": {"post_check": {}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        self.run_engine()
        p.update()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        sorted_checks = list(
            sorted(ctxt.pull_engine_check_runs, key=operator.itemgetter("name"))
        )
        assert len(sorted_checks) == 2
        check = sorted_checks[0]
        assert "action_required" == check["conclusion"]
        assert "Custom checks are disabled" == check["output"]["title"]
