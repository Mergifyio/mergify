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


class TestDismissReviewsAction(base.FunctionalTestBase):
    async def test_dismiss_reviews(self):
        await self._test_dismiss_reviews()

    async def test_dismiss_reviews_custom_message(self):
        await self._test_dismiss_reviews(message="Loser")

    async def _push_for_synchronize(
        self, branch, filename="unwanted_changes", remote="origin"
    ):
        open(self.git.repository + f"/{filename}", "wb").close()
        await self.git("add", self.git.repository + f"/{filename}")
        await self.git("commit", "--no-edit", "-m", filename)
        await self.git("push", "--quiet", remote, branch)

    async def _test_dismiss_reviews_fail(self, msg):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "dismiss_reviews": {
                            "message": msg,
                            "approved": True,
                            "changes_requested": ["mergify-test1"],
                        }
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()
        branch = self.get_full_branch_name(f"integration/pr{self.pr_counter}")
        await self.create_review(p["number"], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

        await self._push_for_synchronize(branch)

        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        ctxt = await context.Context.create(
            self.repository_ctxt,
            p,
        )

        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert "failure" == checks[0]["conclusion"]
        assert (
            "The current Mergify configuration is invalid"
            == checks[0]["output"]["title"]
        )
        return checks[0]

    async def test_dismiss_reviews_custom_message_syntax_error(self):
        check = await self._test_dismiss_reviews_fail("{{Loser")
        assert (
            """Template syntax error @ pull_request_rules → item 0 → actions → dismiss_reviews → message → line 1
```
unexpected end of template, expected 'end of print statement'.
```"""
            == check["output"]["summary"]
        )

    async def test_dismiss_reviews_custom_message_attribute_error(self):
        check = await self._test_dismiss_reviews_fail("{{Loser}}")
        assert (
            """Template syntax error for dictionary value @ pull_request_rules → item 0 → actions → dismiss_reviews → message
```
Unknown pull request attribute: Loser
```"""
            == check["output"]["summary"]
        )

    async def _test_dismiss_reviews(self, message=None):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "dismiss_reviews": {
                            "approved": True,
                            "changes_requested": ["mergify-test1"],
                        }
                    },
                }
            ]
        }

        if message is not None:
            rules["pull_request_rules"][0]["actions"]["dismiss_reviews"][
                "message"
            ] = message

        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()
        await self.create_review(p["number"], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

        await self._push_for_synchronize(p["head"]["ref"])
        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()
        await self.wait_for("pull_request_review", {"action": "dismissed"})

        self.assertEqual(
            [("DISMISSED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

        await self.create_review(p["number"], "REQUEST_CHANGES")

        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("CHANGES_REQUESTED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

        await self._push_for_synchronize(p["head"]["ref"], "unwanted_changes2")
        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()
        await self.wait_for("pull_request_review", {"action": "dismissed"})

        # There's no way to retrieve the dismiss message :(
        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("DISMISSED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

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
                    "event": "action.dismiss_reviews",
                    "metadata": {
                        "users": ["mergify-test1"],
                    },
                    "trigger": "Rule: dismiss reviews",
                },
                {
                    "repository": p["base"]["repo"]["full_name"],
                    "pull_request": p["number"],
                    "timestamp": mock.ANY,
                    "event": "action.dismiss_reviews",
                    "metadata": {
                        "users": ["mergify-test1"],
                    },
                    "trigger": "Rule: dismiss reviews",
                },
            ],
            "per_page": 10,
            "size": 2,
            "total": 2,
        }
        return p

    async def test_dismiss_reviews_ignored(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "dismiss_reviews": {
                            "approved": True,
                        }
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()
        await self.create_review(p["number"], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

        # Move base branch
        await self.git("checkout", self.main_branch_name)
        await self._push_for_synchronize(self.main_branch_name)
        await self.run_engine()

        await self.create_comment_as_admin(p["number"], "@mergifyio refresh")
        await self.run_engine()

        # Ensure review have not been dismiss
        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )

    async def test_dismiss_reviews_from_requested_reviewers(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "dismiss_reviews": {
                            "when": "always",
                            "approved": "from_requested_reviewers",
                        }
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()
        await self.create_review(p["number"], "APPROVE")
        await self.create_review(
            p["number"], "APPROVE", oauth_token=config.ORG_USER_PERSONAL_TOKEN
        )

        self.assertEqual(
            [("APPROVED", "mergify-test1"), ("APPROVED", "mergify-test4")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )
        await self.create_review_request(p["number"], ["mergify-test1"])
        await self.run_engine()

        # Ensure review have been dismiss
        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("APPROVED", "mergify-test4")],
            [
                (r["state"], r["user"]["login"])
                for r in await self.get_reviews(p["number"])
            ],
        )
