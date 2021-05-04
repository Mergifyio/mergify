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
    async def test_assign_with_users(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"users": ["mergify-test1"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test1"]),
            sorted(user["login"] for user in pulls[0]["assignees"]),
        )

    async def test_assign_with_add_users(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"add_users": ["mergify-test1"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test1"]),
            sorted(user["login"] for user in pulls[0]["assignees"]),
        )

    async def test_assign_valid_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"users": ["{{author}}"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()

        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test2"]),
            sorted(user["login"] for user in pulls[0]["assignees"]),
        )

    async def test_assign_user_already_assigned(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"add_users": ["mergify-test1"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.add_assignee(p["number"], "mergify-test1")
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test1"]),
            sorted(user["login"] for user in pulls[0]["assignees"]),
        )

    async def test_remove_assignee(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"assign": {"remove_users": ["mergify-test1"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.add_assignee(p["number"], "mergify-test1")
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            [],
            pulls[0]["assignees"],
        )
