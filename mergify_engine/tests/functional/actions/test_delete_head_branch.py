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

from mergify_engine.tests.functional import base


class TestDeleteHeadBranchAction(base.FunctionalTestBase):
    def test_delete_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": None},
                },
                {
                    "name": "delete on close",
                    "conditions": ["base=master", "label=close", "closed"],
                    "actions": {"delete_head_branch": {}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p2, _ = self.create_pr(base_repo="main", branch="#2-second-pr")
        self.add_label(p1, "merge")
        self.add_label(p2, "close")

        p1.merge()
        self.wait_for("pull_request", {"action": "closed"})

        p2.edit(state="close")
        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(2, len(pulls))

        branches = list(self.r_o_admin.get_branches())
        self.assertEqual(1, len(branches))
        self.assertEqual("master", branches[0].name)

    def test_delete_branch_with_dep_no_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": None},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p2, _ = self.create_pr(
            base_repo="main", branch="#2-second-pr", base="#1-first-pr"
        )

        p1.merge()
        self.wait_for("pull_request", {"action": "closed"})
        self.add_label(p1, "merge")
        self.wait_for("check_run", {"check_run": {"conclusion": "success"}})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        assert 2 == len(pulls)

        branches = list(self.r_o_admin.get_branches())
        assert 3 == len(branches)
        assert {"master", "#1-first-pr", "#2-second-pr"} == {b.name for b in branches}

    def test_delete_branch_with_dep_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": {"force": True}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p2, _ = self.create_pr(
            base_repo="main", branch="#2-second-pr", base="#1-first-pr"
        )

        p1.merge()
        self.wait_for("pull_request", {"action": "closed", "number": p1.number})
        self.add_label(p1, "merge")
        self.wait_for("pull_request", {"action": "closed", "number": p2.number})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        assert 2 == len(pulls)

        branches = list(self.r_o_admin.get_branches())
        assert 2 == len(branches)
        assert {"master", "#2-second-pr"} == {b.name for b in branches}
