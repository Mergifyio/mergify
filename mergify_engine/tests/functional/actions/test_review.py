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
from unittest import mock

import yaml

from mergify_engine import config
from mergify_engine import context
from mergify_engine.tests.functional import base


class TestReviewAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_review(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "approve",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"review": {"type": "APPROVE"}},
                },
                {
                    "name": "requested",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "review": {"message": "WTF?", "type": "REQUEST_CHANGES"}
                    },
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(as_="fork")
        await self.run_engine()
        await self.wait_for("pull_request_review", {}),

        await self.run_engine()
        await self.wait_for("pull_request_review", {}),

        reviews = await self.get_reviews(p["number"])
        self.assertEqual(2, len(reviews))
        self.assertEqual("APPROVED", reviews[-2]["state"])
        self.assertEqual("CHANGES_REQUESTED", reviews[-1]["state"])
        self.assertEqual("WTF?", reviews[-1]["body"])

    async def test_review_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "approve",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"review": {"type": "APPROVE"}},
                },
                {
                    "name": "requested",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "review": {
                            "message": "WTF {{author}}?",
                            "type": "REQUEST_CHANGES",
                        }
                    },
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(as_="fork")
        await self.run_engine()

        await self.wait_for("pull_request_review", {}),
        await self.run_engine()

        reviews = await self.get_reviews(p["number"])
        self.assertEqual(2, len(reviews))
        self.assertEqual("APPROVED", reviews[-2]["state"])
        self.assertEqual("CHANGES_REQUESTED", reviews[-1]["state"])
        self.assertEqual("WTF mergify-test2?", reviews[-1]["body"])

    async def _test_review_template_error(self, msg):
        rules = {
            "pull_request_rules": [
                {
                    "name": "review",
                    "conditions": [
                        f"base={self.main_branch_name}",
                    ],
                    "actions": {"review": {"message": msg, "type": "REQUEST_CHANGES"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(as_="fork")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert "failure" == checks[0]["conclusion"]
        assert (
            "The current Mergify configuration is invalid"
            == checks[0]["output"]["title"]
        )
        return checks[0]

    async def test_review_template_syntax_error(self):
        check = await self._test_review_template_error(
            msg="Thank you {{",
        )
        assert (
            """Template syntax error @ pull_request_rules → item 0 → actions → review → message → line 1
```
unexpected 'end of template'
```"""
            == check["output"]["summary"]
        )

    async def test_review_template_attribute_error(self):
        check = await self._test_review_template_error(
            msg="Thank you {{hello}}",
        )
        assert (
            """Template syntax error for dictionary value @ pull_request_rules → item 0 → actions → review → message
```
Unknown pull request attribute: hello
```"""
            == check["output"]["summary"]
        )

    async def test_review_with_oauth_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "approve",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "review": {
                            "type": "APPROVE",
                        }
                    },
                },
                {
                    "name": "requested",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "review": {
                            "message": "WTF?",
                            "type": "REQUEST_CHANGES",
                            "bot_account": "mergify-test4",
                        }
                    },
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(as_="fork", message="mergify-test4")
        await self.run_engine()

        await self.wait_for("pull_request_review", {}),
        await self.run_engine()

        reviews = await self.get_reviews(p["number"])
        self.assertEqual(2, len(reviews))
        self.assertEqual("APPROVED", reviews[-2]["state"])
        self.assertEqual("CHANGES_REQUESTED", reviews[-1]["state"])
        self.assertEqual("WTF?", reviews[-1]["body"])
        self.assertEqual("mergify-test4", reviews[-1]["user"]["login"])

        # ensure review don't get posted twice
        await self.create_comment(p["number"], "@mergifyio refresh")
        await self.run_engine()
        reviews = await self.get_reviews(p["number"])
        self.assertEqual(2, len(reviews))

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": [
                {
                    "repository": p["base"]["repo"]["full_name"],
                    "pull_request": p["number"],
                    "timestamp": mock.ANY,
                    "event": "action.review",
                    "metadata": {
                        "type": "REQUEST_CHANGES",
                        "reviewer": "mergify-test4",
                    },
                    "trigger": "Rule: requested",
                },
                {
                    "repository": p["base"]["repo"]["full_name"],
                    "pull_request": p["number"],
                    "timestamp": mock.ANY,
                    "event": "action.review",
                    "metadata": {
                        "type": "APPROVE",
                        "reviewer": config.BOT_USER_LOGIN,
                    },
                    "trigger": "Rule: approve",
                },
            ],
            "per_page": 10,
            "size": 2,
            "total": 2,
        }
