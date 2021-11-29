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
import yaml

from mergify_engine import context
from mergify_engine.tests.functional import base


class TestCommentActionWithSub(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_comment_with_bot_account(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "comment": {"message": "WTF?", "bot_account": "{{ body }}"}
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(message="mergify-test4")
        await self.run_engine()

        p = await self.get_pull(p["number"])
        comments = await self.get_issue_comments(p["number"])
        assert comments[-1]["body"] == "WTF?"
        assert comments[-1]["user"]["login"] == "mergify-test4"


class TestCommentAction(base.FunctionalTestBase):
    async def test_comment(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        comments = await self.get_issue_comments(p["number"])
        assert comments[-1]["body"] == "WTF?"

        # Add a label to trigger mergify
        await self.add_label(p["number"], "stable")
        await self.run_engine()

        # Ensure nothing changed
        new_comments = await self.get_issue_comments(p["number"])
        assert new_comments[-1]["body"] == "WTF?"

        # Add new commit to ensure Summary get copied and comment not reposted
        open(self.git.tmp + "/new_file", "wb").close()
        await self.git("add", self.git.tmp + "/new_file")
        await self.git("commit", "--no-edit", "-m", "new commit")
        await self.git(
            "push",
            "--quiet",
            "fork",
            self.get_full_branch_name(f"fork/pr{self.pr_counter}"),
        )

        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()

        # Ensure nothing changed
        new_comments = await self.get_issue_comments(p["number"])
        self.assertEqual(len(comments), len(new_comments))
        assert new_comments[-1]["body"] == "WTF?"

    async def test_comment_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"comment": {"message": "Thank you {{author}}"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()

        new_comments = await self.get_issue_comments(p["number"])
        assert new_comments[-1]["body"] == "Thank you mergify-test2"

    async def test_comment_with_none(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"comment": {"message": None}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()
        p = await self.get_pull(p["number"])

        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert "success" == checks[0]["conclusion"]

        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 0

    async def _test_comment_template_error(self, msg):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"comment": {"message": msg}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()
        p = await self.get_pull(p["number"])

        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert "failure" == checks[0]["conclusion"]
        assert "The Mergify configuration is invalid" == checks[0]["output"]["title"]
        return checks[0]

    async def test_comment_template_syntax_error(self):
        check = await self._test_comment_template_error(
            msg="Thank you {{",
        )
        assert (
            """Template syntax error @ pull_request_rules → item 0 → actions → comment → message → line 1
```
unexpected 'end of template'
```"""
            == check["output"]["summary"]
        )

    async def test_comment_template_attribute_error(self):
        check = await self._test_comment_template_error(
            msg="Thank you {{hello}}",
        )
        assert (
            """Template syntax error for dictionary value @ pull_request_rules → item 0 → actions → comment → message
```
Unknown pull request attribute: hello
```"""
            == check["output"]["summary"]
        )

    async def test_comment_with_bot_account(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "comment": {"message": "WTF?", "bot_account": "mergify-test4"}
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])

        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 0

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert (
            checks[-1]["output"]["title"]
            == "Comments with `bot_account` set are disabled"
        )
