# -*- encoding: utf-8 -*-
#
#  Copyright © 2021–2022 Mergify SAS
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

import pytest
import yaml

from mergify_engine import config
from mergify_engine.tests.functional import base


class TestEditAction(base.FunctionalTestBase):
    @pytest.mark.skipif(
        not config.GITHUB_URL.startswith("https://github.com"),
        reason="requires GHES 3.2",
    )
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

        p = await self.create_pr()
        assert p["draft"] is False
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is True

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": [
                {
                    "event": "action.edit",
                    "pull_request": p["number"],
                    "metadata": {"draft": True},
                    "timestamp": mock.ANY,
                    "trigger": "Rule: convert Pull Request to Draft",
                    "repository": p["base"]["repo"]["full_name"],
                },
            ],
            "per_page": 10,
            "size": 1,
            "total": 1,
        }

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

        p = await self.create_pr(draft=True)
        assert p["draft"] is True
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is False

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": [
                {
                    "event": "action.edit",
                    "pull_request": p["number"],
                    "metadata": {"draft": False},
                    "timestamp": mock.ANY,
                    "trigger": "Rule: convert Pull Request to Draft",
                    "repository": p["base"]["repo"]["full_name"],
                },
            ],
            "per_page": 10,
            "size": 1,
            "total": 1,
        }

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

        p = await self.create_pr(draft=True)
        assert p["draft"] is True
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is True

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": [],
            "per_page": 10,
            "size": 0,
            "total": 0,
        }

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

        p = await self.create_pr(draft=False)
        assert p["draft"] is False
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))

        p = await self.get_pull(p["number"])
        assert p["draft"] is False

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": [],
            "per_page": 10,
            "size": 0,
            "total": 0,
        }
