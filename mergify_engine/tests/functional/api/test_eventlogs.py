# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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

from mergify_engine import config
from mergify_engine.tests.functional import base


class TestEventLogsAction(base.FunctionalTestBase):
    async def test_eventlogs(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "hello",
                    "conditions": [f"base={self.main_branch_name}", "label=auto-merge"],
                    "actions": {
                        "comment": {
                            "message": "Hello!",
                        },
                    },
                },
                {
                    "name": "mergeit",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "assign": {"users": ["mergify-test1"]},
                        "label": {
                            "add": ["need-review"],
                            "remove": ["auto-merge"],
                        },
                        "merge": {},
                    },
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        await self.add_label(p1["number"], "auto-merge")
        await self.run_engine()

        p1_expected_events = [
            {
                "repository": p1["base"]["repo"]["full_name"],
                "pull_request": p1["number"],
                "timestamp": mock.ANY,
                "event": "action.comment",
                "metadata": {},
                "trigger": "Rule: hello",
            },
            {
                "repository": p1["base"]["repo"]["full_name"],
                "pull_request": p1["number"],
                "timestamp": mock.ANY,
                "event": "action.assign",
                "metadata": {
                    "added": ["mergify-test1"],
                    "removed": [],
                },
                "trigger": "Rule: mergeit",
            },
            {
                "repository": p1["base"]["repo"]["full_name"],
                "pull_request": p1["number"],
                "timestamp": mock.ANY,
                "event": "action.label",
                "metadata": {
                    "added": ["need-review"],
                    "removed": ["auto-merge"],
                },
                "trigger": "Rule: mergeit",
            },
            {
                "repository": p1["base"]["repo"]["full_name"],
                "pull_request": p1["number"],
                "timestamp": mock.ANY,
                "event": "action.merge",
                "metadata": {},
                "trigger": "Rule: mergeit",
            },
        ]
        p2_expected_events = [
            {
                "repository": p2["base"]["repo"]["full_name"],
                "pull_request": p2["number"],
                "timestamp": mock.ANY,
                "event": "action.assign",
                "metadata": {
                    "added": ["mergify-test1"],
                    "removed": [],
                },
                "trigger": "Rule: mergeit",
            },
            {
                "repository": p2["base"]["repo"]["full_name"],
                "pull_request": p2["number"],
                "timestamp": mock.ANY,
                "event": "action.label",
                "metadata": {
                    "added": ["need-review"],
                    "removed": [],
                },
                "trigger": "Rule: mergeit",
            },
            {
                "repository": p2["base"]["repo"]["full_name"],
                "pull_request": p2["number"],
                "timestamp": mock.ANY,
                "event": "action.merge",
                "metadata": {},
                "trigger": "Rule: mergeit",
            },
        ]

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p1['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"events": p1_expected_events}

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p2['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"events": p2_expected_events}

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {"events": p1_expected_events + p2_expected_events}
