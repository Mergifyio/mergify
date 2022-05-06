# -*- encoding: utf-8 -*-
#
#  Copyright © 2021 Mergify SAS
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


class TestRebaseAction(base.FunctionalTestBase):
    async def test_rebase_ok(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"rebase": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        pr_initial_sha = p["head"]["sha"]

        await self.git("checkout", self.main_branch_name)

        open(self.git.repository + "/an_other_file", "wb").close()
        await self.git("add", "an_other_file")

        await self.git("commit", "--no-edit", "-m", "commit ahead")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        p = await self.get_pull(p["number"])

        final_sha = p["head"]["sha"]

        self.assertNotEqual(pr_initial_sha, final_sha)

    async def test_rebase_on_closed_pr_deleted_branch(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"rebase": {}},
                },
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}", "label=merge"],
                    "actions": {"merge": {}, "delete_head_branch": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 1
        await self.add_label(p1["number"], "merge")

        await self.run_engine()
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 1
        # Now merge p2 so p1 is not up to date
        await self.add_label(p2["number"], "merge")
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        checks = await ctxt.pull_engine_check_runs
        for check in checks:
            assert check["conclusion"] == "success", check

    async def test_rebase_on_uptodate_pr_branch(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.main_branch_name}", "label=rebase"],
                    "actions": {"rebase": {}},
                },
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}", "label=merge"],
                    "actions": {"merge": {}, "delete_head_branch": {}},
                },
                {
                    "name": "update",
                    "conditions": [f"base={self.main_branch_name}", "label=update"],
                    "actions": {"update": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        await self.add_label(p1["number"], "merge")
        await self.run_engine()
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 1

        # Now update p2 with a merge from the base branch
        await self.add_label(p2["number"], "update")
        await self.run_engine()
        await self.remove_label(p2["number"], "update")
        p2_updated = await self.get_pull(p2["number"])

        # Now rebase p2
        await self.add_label(p2["number"], "rebase")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        p2_rebased = await self.get_pull(p2["number"])
        assert p2_updated["head"]["sha"] != p2_rebased["head"]["sha"]

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        checks = await ctxt.pull_engine_check_runs
        for check in checks:
            assert check["conclusion"] == "success", check
