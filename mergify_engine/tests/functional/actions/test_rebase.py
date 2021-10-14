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
    async def test_rebase_ok(self):
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

        p, commits = await self.create_pr()
        pr_initial_sha = commits[-1]["sha"]

        await self.git("checkout", self.main_branch_name)

        open(self.git.tmp + "/an_other_file", "wb").close()
        await self.git("add", "an_other_file")

        await self.git("commit", "--no-edit", "-m", "commit ahead")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        await self.run_engine(1)
        p = await self.get_pull(p["number"])

        final_sha = p["head"]["sha"]

        self.assertNotEqual(pr_initial_sha, final_sha)

    async def test_rebase_on_closed_pr_deleted_branch(self):
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

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
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
