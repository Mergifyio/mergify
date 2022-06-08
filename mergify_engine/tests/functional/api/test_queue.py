# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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
from mergify_engine.tests.functional import base


class TestQueueApi(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_invalid_rules_in_config(self) -> None:
        invalid_rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                    "batch_max_wait_time": "15 s",
                    "speculative_checks": 1,
                    "false_condition": 3,
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "false_condition": 2,
                    "batch_size": 2,
                    "batch_max_wait_time": "0 s",
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "allow_inplace_checks": False,
                    "checks_timeout": "10 m",
                    "false_condition": "mergify-test4",
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue-urgent",
                    ],
                    "actions": {"false_action": {"name": "urgent"}},
                },
                {
                    "name": "Merge default",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"false_action": {"name": "default"}},
                },
                {
                    "name": "Merge low",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue-low",
                    ],
                    "actions": {"false_action": {"name": "low-priority"}},
                },
            ],
        }

        await self.setup_repo(yaml.dump(invalid_rules))

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/configuration",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 422
        assert r.json() == {"detail": "The configuration file is invalid."}

    async def test_get_queues_config_list(self) -> None:
        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                    "batch_max_wait_time": "15 s",
                    "speculative_checks": 1,
                    "batch_size": 3,
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "speculative_checks": 2,
                    "batch_size": 2,
                    "batch_max_wait_time": "0 s",
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "allow_inplace_checks": False,
                    "checks_timeout": "10 m",
                    "draft_bot_account": "mergify-test4",
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue-urgent",
                    ],
                    "actions": {"queue": {"name": "urgent"}},
                },
                {
                    "name": "Merge default",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
                {
                    "name": "Merge low",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue-low",
                    ],
                    "actions": {"queue": {"name": "low-priority"}},
                },
            ],
        }

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/configuration",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {"configuration": []}

        await self.setup_repo(yaml.dump(rules))

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/configuration",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "configuration": [
                {
                    "name": "urgent",
                    "config": {
                        "allow_inplace_checks": True,
                        "disallow_checks_interruption_from_queues": [],
                        "batch_size": 3,
                        "batch_max_wait_time": 15.0,
                        "checks_timeout": None,
                        "draft_bot_account": None,
                        "priority": 2,
                        "speculative_checks": 1,
                    },
                },
                {
                    "name": "default",
                    "config": {
                        "allow_inplace_checks": True,
                        "disallow_checks_interruption_from_queues": [],
                        "batch_size": 2,
                        "batch_max_wait_time": 0.0,
                        "checks_timeout": None,
                        "draft_bot_account": None,
                        "priority": 1,
                        "speculative_checks": 2,
                    },
                },
                {
                    "name": "low-priority",
                    "config": {
                        "allow_inplace_checks": False,
                        "disallow_checks_interruption_from_queues": [],
                        "batch_size": 1,
                        "batch_max_wait_time": 30.0,
                        "checks_timeout": 600.0,
                        "draft_bot_account": "mergify-test4",
                        "priority": 0,
                        "speculative_checks": 1,
                    },
                },
            ]
        }
