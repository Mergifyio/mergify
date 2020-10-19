# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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


class TestAssignAction(base.FunctionalTestBase):
    def test_assign(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"users": ["mergify-test1"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test1"]),
            sorted([user.login for user in pulls[0].assignees]),
        )

    def test_assign_valid_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"users": ["{{author}}"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted([self.u_fork.login]),
            sorted([user.login for user in pulls[0].assignees]),
        )
