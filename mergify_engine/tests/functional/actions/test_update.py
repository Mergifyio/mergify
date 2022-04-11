# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
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

from mergify_engine import config
from mergify_engine import context
from mergify_engine.tests.functional import base


class TestUpdateAction(base.FunctionalTestBase):
    async def test_update_action(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "update",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"update": {}},
                },
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}", "label=merge"],
                    "actions": {"merge": {}},
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
        await self.wait_for("pull_request", {"action": "closed"})
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 2
        assert commits[-1]["commit"]["author"]["name"] == config.BOT_USER_LOGIN
        assert commits[-1]["commit"]["message"].startswith("Merge branch")

    async def test_update_action_on_closed_pr_deleted_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "update",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"update": {}},
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
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 2
        assert commits[-1]["commit"]["author"]["name"] == config.BOT_USER_LOGIN
        assert commits[-1]["commit"]["message"].startswith("Merge branch")
        # Now merge p2 so p1 is not up to date
        await self.add_label(p2["number"], "merge")
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        checks = await ctxt.pull_engine_check_runs
        for check in checks:
            assert check["conclusion"] == "success", check
