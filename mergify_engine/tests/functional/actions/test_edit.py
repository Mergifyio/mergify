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


class TestEditAction(base.FunctionalTestBase):
    async def test_pr_to_draft_edit(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "convert Pull Request to Draft",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "edit": {"draft": True},
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        assert p["draft"] is False
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is True

    async def test_draft_to_ready_for_review(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "convert Pull Request to Draft",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "edit": {"draft": False},
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(draft=True)
        assert p["draft"] is True
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is False

    async def test_draft_already_converted(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "convert Pull Request to Draft",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "edit": {"draft": True},
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(draft=True)
        assert p["draft"] is True
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is True

    async def test_ready_for_review_already_converted(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "convert Pull Request to Draft",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "edit": {"draft": False},
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(draft=False)
        assert p["draft"] is False
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is False
