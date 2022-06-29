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
from mergify_engine import signals
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
                "event": "action.merge",
                "metadata": {},
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
                "event": "action.comment",
                "metadata": {},
                "trigger": "Rule: hello",
            },
        ]
        p2_expected_events = [
            {
                "repository": p2["base"]["repo"]["full_name"],
                "pull_request": p2["number"],
                "timestamp": mock.ANY,
                "event": "action.merge",
                "metadata": {},
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
                "event": "action.assign",
                "metadata": {
                    "added": ["mergify-test1"],
                    "removed": [],
                },
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
        assert r.json() == {
            "events": p1_expected_events,
            "per_page": 10,
            "size": 4,
            "total": 4,
        }

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p2['number']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": p2_expected_events,
            "per_page": 10,
            "size": 3,
            "total": 3,
        }

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": p2_expected_events + p1_expected_events,
            "per_page": 10,
            "size": 7,
            "total": 7,
        }

        # pagination
        r_pagination = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/events?per_page=2",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_pagination.status_code == 200
        assert r_pagination.json() == {
            "events": p2_expected_events[:2],
            "per_page": 2,
            "size": 2,
            "total": 7,
        }

        # first page
        r_first = await self.app.get(
            r_pagination.links["first"]["url"],
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_first.status_code == 200
        assert r_first.json() == {
            "events": p2_expected_events[:2],
            "per_page": 2,
            "size": 2,
            "total": 7,
        }
        # next page
        r_next = await self.app.get(
            r_pagination.links["next"]["url"],
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_next.status_code == 200
        assert r_next.json() == {
            "events": [p2_expected_events[2], p1_expected_events[0]],
            "per_page": 2,
            "size": 2,
            "total": 7,
        }
        # next next
        r_next_next = await self.app.get(
            r_next.links["next"]["url"],
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_next_next.status_code == 200
        assert r_next_next.json() == {
            "events": p1_expected_events[1:3],
            "per_page": 2,
            "size": 2,
            "total": 7,
        }
        # prev
        r_prev = await self.app.get(
            r_next.links["prev"]["url"],
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_prev.status_code == 200
        assert r_prev.json() == {
            "events": p2_expected_events[:2],
            "per_page": 2,
            "size": 2,
            "total": 7,
        }

        # last
        r_last = await self.app.get(
            r_pagination.links["last"]["url"],
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r_last.status_code == 200
        assert r_last.json() == {
            "events": [p1_expected_events[3]],
            "per_page": 2,
            "size": 1,
            "total": 7,
        }

    async def test_incomplete_eventlogs_metadata(self):
        await self.setup_repo()

        expected_events = [
            {
                "repository": self.repository_ctxt.repo["full_name"],
                "pull_request": 123,
                "timestamp": mock.ANY,
                "event": "action.queue.merged",
                "metadata": {},
                "trigger": "gogogo",
            },
        ]

        # We don't send any metadata on purpose
        await signals.send(
            self.repository_ctxt, 123, "action.queue.merged", {}, "gogogo"
        )
        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/123/events",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "events": expected_events,
            "per_page": 10,
            "size": 1,
            "total": 1,
        }
