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

from mergify_engine.tests.functional import base


class TestRebaseAction(base.FunctionalTestBase):
    def test_rebase_ok(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"rebase": {}},
                }
            ]
        }

        ahead_branch = self.get_full_branch_name("main/head")

        self.setup_repo(
            yaml.dump(rules),
            test_branches=[
                ahead_branch,
            ],
        )

        p, commits = self.create_pr()
        tmp_sha = commits[-1].sha

        self.git("checkout", ahead_branch)

        open(self.git.tmp + "/an_other_file", "wb").close()
        self.git("add", "an_other_file")

        self.git("commit", "--no-edit", "-m", "commit ahead")
        self.git("push", "--quiet", "main", self.master_branch_name)

        self.run_engine()
        p.update()
        final_sha = p.head.sha

        self.assertEqual("closed", p.state)
        self.assertNotEqual(tmp_sha, final_sha)
