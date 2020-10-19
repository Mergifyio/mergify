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


class TestDismissReviewsAction(base.FunctionalTestBase):
    def test_dismiss_reviews(self):
        return self._test_dismiss_reviews()

    def test_dismiss_reviews_custom_message(self):
        return self._test_dismiss_reviews(message="Loser")

    def _push_for_synchronize(self, branch, filename="unwanted_changes"):
        open(self.git.tmp + f"/{filename}", "wb").close()
        self.git("add", self.git.tmp + f"/{filename}")
        self.git("commit", "--no-edit", "-m", filename)
        self.git("push", "--quiet", "fork", branch)

    def _test_dismiss_reviews_fail(self, msg):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.master_branch_name}"],
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

        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()
        branch = self.get_full_branch_name("fork/pr%d" % self.pr_counter)
        self.create_review(p, commits[-1], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        self._push_for_synchronize(branch)

        self.wait_for("pull_request", {"action": "synchronize"})
        self.run_engine()
        p.update()

        ctxt = context.Context(
            self.cli_integration,
            p.raw_data,
            None,
        )

        assert len(ctxt.pull_engine_check_runs) == 1
        check = ctxt.pull_engine_check_runs[0]
        assert "failure" == check["conclusion"]
        assert "The Mergify configuration is invalid" == check["output"]["title"]
        return check

    def test_dismiss_reviews_custom_message_syntax_error(self):
        check = self._test_dismiss_reviews_fail("{{Loser")
        assert (
            """Template syntax error @ data['pull_request_rules'][0]['actions']['dismiss_reviews']['message'][line 1]
```
unexpected end of template, expected 'end of print statement'.
```"""
            == check["output"]["summary"]
        )

    def test_dismiss_reviews_custom_message_attribute_error(self):
        check = self._test_dismiss_reviews_fail("{{Loser}}")
        assert (
            """Template syntax error for dictionary value @ data['pull_request_rules'][0]['actions']['dismiss_reviews']['message']
```
Unknown pull request attribute: Loser
```"""
            == check["output"]["summary"]
        )

    def _test_dismiss_reviews(self, message=None):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": [f"base={self.master_branch_name}"],
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

        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()
        branch = self.get_full_branch_name("fork/pr%d" % self.pr_counter)
        self.create_review(p, commits[-1], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        self._push_for_synchronize(branch)
        self.wait_for("pull_request", {"action": "synchronize"})

        self.run_engine()
        self.wait_for("pull_request_review", {"action": "dismissed"})

        self.assertEqual(
            [("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        commits = list(p.get_commits())
        self.create_review(p, commits[-1], "REQUEST_CHANGES")

        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("CHANGES_REQUESTED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        self._push_for_synchronize(branch, "unwanted_changes2")
        self.wait_for("pull_request", {"action": "synchronize"})

        self.run_engine()
        self.wait_for("pull_request_review", {"action": "dismissed"})

        # There's no way to retrieve the dismiss message :(
        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        return p
