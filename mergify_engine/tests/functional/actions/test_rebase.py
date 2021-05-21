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
from unittest import mock

import yaml

from mergify_engine.tests.functional import base


class TestRebaseAction(base.FunctionalTestBase):
    async def _do_test_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"rebase": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, commits = await self.create_pr()
        pr_initial_sha = commits[-1]["sha"]

        await self.git("checkout", self.master_branch_name)

        open(self.git.tmp + "/an_other_file", "wb").close()
        await self.git("add", "an_other_file")

        await self.git("commit", "--no-edit", "-m", "commit ahead")
        await self.git("push", "--quiet", "main", self.master_branch_name)
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})

        await self.run_engine(1)
        p = await self.get_pull(p["number"])

        final_sha = p["head"]["sha"]
        return pr_initial_sha, final_sha

    async def test_rebase_ok(self):
        pr_initial_sha, final_sha = await self._do_test_rebase()
        self.assertNotEqual(pr_initial_sha, final_sha)

    async def test_rebase_fail_heavy_repo(self):
        with mock.patch("mergify_engine.config.NOSUB_MAX_REPO_SIZE_KB", -1):
            pr_initial_sha, final_sha = await self._do_test_rebase()
            self.assertEqual(pr_initial_sha, final_sha)
