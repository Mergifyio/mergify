# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Julien Danjou <jd@mergify.io>
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

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine.tests.functional import base


class TestCommentAction(base.FunctionalTestBase):
    def test_comment(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        p.update()
        comments = list(p.get_issue_comments())
        self.assertEqual("WTF?", comments[-1].body)

        # Add a label to trigger mergify
        self.add_label(p, "stable")

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)

        # Add new commit to ensure Summary get copied and comment not reposted
        open(self.git.tmp + "/new_file", "wb").close()
        self.git("add", self.git.tmp + "/new_file")
        self.git("commit", "--no-edit", "-m", "new commit")
        self.git(
            "push",
            "--quiet",
            "fork",
            self.get_full_branch_name("fork/pr%d" % self.pr_counter),
        )

        self.wait_for("pull_request", {"action": "synchronize"})

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)

    def test_comment_backwardcompat(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        p.update()
        comments = list(p.get_issue_comments())
        self.assertEqual("WTF?", comments[-1].body)

        # Override Summary with the old format
        pull = context.Context(self.cli_integration, p.raw_data, {})
        check_api.set_check_run(
            pull,
            "Summary",
            "completed",
            "success",
            output={"title": "whatever", "summary": "erased"},
        )

        # Add a label to trigger mergify
        self.add_label(p, "stable")

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)
