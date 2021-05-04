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
    async def test_delete_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=merge",
                        "merged",
                    ],
                    "actions": {"delete_head_branch": None},
                },
                {
                    "name": "delete on close",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=close",
                        "closed",
                    ],
                    "actions": {"delete_head_branch": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        first_branch = self.get_full_branch_name("#1-first-pr")
        second_branch = self.get_full_branch_name("#2-second-pr")
        p1, _ = await self.create_pr(base_repo="main", branch=first_branch)
        p2, _ = await self.create_pr(base_repo="main", branch=second_branch)
        await self.add_label(p1["number"], "merge")
        await self.add_label(p2["number"], "close")

        await self.merge_pull(p1["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.edit_pull(p2["number"], state="close")
        await self.wait_for("pull_request", {"action": "closed"})

        await self.run_engine()

        pulls = await self.get_pulls(
            params={"state": "all", "base": self.master_branch_name}
        )
        self.assertEqual(2, len(pulls))

        branches = await self.get_branches()
        self.assertEqual(2, len(branches))
        self.assertEqual(self.master_branch_name, branches[0]["name"])
        self.assertEqual("master", branches[1]["name"])

    async def test_delete_branch_with_dep_no_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=merge",
                        "merged",
                    ],
                    "actions": {"delete_head_branch": None},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        first_branch = self.get_full_branch_name("#1-first-pr")
        second_branch = self.get_full_branch_name("#2-second-pr")
        p1, _ = await self.create_pr(base_repo="main", branch=first_branch)
        p2, _ = await self.create_pr(
            base_repo="main", branch=second_branch, base=first_branch
        )

        await self.merge_pull(p1["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.add_label(p1["number"], "merge")
        await self.run_engine()

        await self.wait_for("check_run", {"check_run": {"conclusion": "neutral"}})

        pulls = await self.get_pulls(
            params={"state": "all", "base": self.master_branch_name}
        )
        assert 1 == len(pulls)
        pulls = await self.get_pulls(params={"state": "all", "base": first_branch})
        assert 1 == len(pulls)

        branches = await self.get_branches()
        assert 4 == len(branches)
        assert {"master", self.master_branch_name, first_branch, second_branch} == {
            b["name"] for b in branches
        }

    async def test_delete_branch_with_dep_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=merge",
                        "merged",
                    ],
                    "actions": {"delete_head_branch": {"force": True}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        first_branch = self.get_full_branch_name("#1-first-pr")
        second_branch = self.get_full_branch_name("#2-second-pr")
        p1, _ = await self.create_pr(base_repo="main", branch=first_branch)
        p2, _ = await self.create_pr(
            base_repo="main", branch=second_branch, base=first_branch
        )

        await self.merge_pull(p1["number"])
        await self.wait_for(
            "pull_request", {"action": "closed", "number": p1["number"]}
        )
        await self.add_label(p1["number"], "merge")
        await self.run_engine()
        await self.wait_for(
            "pull_request", {"action": "closed", "number": p2["number"]}
        )
        await self.run_engine()

        pulls = await self.get_pulls(
            params={"state": "all", "base": self.master_branch_name}
        )
        assert 1 == len(pulls)
        pulls = await self.get_pulls(params={"state": "all", "base": first_branch})
        assert 1 == len(pulls)

        branches = await self.get_branches()
        assert 3 == len(branches)
        assert {"master", self.master_branch_name, second_branch} == {
            b["name"] for b in branches
        }
