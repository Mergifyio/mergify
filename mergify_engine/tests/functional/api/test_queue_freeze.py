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
from unittest import mock

from first import first
import yaml

from mergify_engine import config
from mergify_engine import context
from mergify_engine.queue import freeze
from mergify_engine.tests.functional import base


class TestQueueFreeze(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_request_error_create_queue_freeze(self):
        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))
        queue_name = "false_queue_name"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=None,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 422
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body"],
                    "msg": "field required",
                    "type": "value_error.missing",
                }
            ]
        }

        freeze_payload = {"false_key": "test freeze reason"}

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 422
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "reason"],
                    "msg": "field required",
                    "type": "value_error.missing",
                }
            ]
        }

        freeze_payload = {"reason": "too long" * 100}

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/default/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 422
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "reason"],
                    "msg": "ensure this value has at most 255 characters",
                    "type": "value_error.any_str.max_length",
                    "ctx": {"limit_value": 255},
                }
            ]
        }

        freeze_payload = {"reason": "test freeze reason"}

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 404
        assert r.json() == {"detail": f'The queue "{queue_name}" does not exist.'}

    async def test_create_queue_freeze(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))
        p1 = await self.create_pr()
        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        freeze_payload = {"reason": "test freeze reason"}
        queue_name = "default"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        queue_freeze_data_default = await freeze.QueueFreeze.get(
            self.repository_ctxt, "default"
        )

        assert queue_freeze_data_default is not None
        assert queue_freeze_data_default.reason == "test freeze reason"

        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge default (queue)",
        )

        assert (
            check["output"]["title"]
            == 'The queue "default" is currently frozen, for the following reason: test freeze reason'
        )

        p2 = await self.create_pr()
        p3 = await self.create_pr()

        await self.add_label(p2["number"], "queue-urgent")
        await self.add_label(p3["number"], "queue-low")
        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )

        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        check = first(
            await context.Context(self.repository_ctxt, p3).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge low (queue)",
        )

        assert (
            check["output"]["title"]
            == "The pull request is the 3rd in the queue to be merged"
        )

        # merge p1
        await self.create_status(p2, context="continuous-integration/fast-ci")
        await self.run_engine()
        p2 = await self.get_pull(p2["number"])
        assert p2["merged"]

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )

        assert (
            check["output"]["title"] == "The pull request has been merged automatically"
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge default (queue)",
        )

        assert (
            check["output"]["title"]
            == 'The queue "default" is currently frozen, for the following reason: test freeze reason'
        )

        check = first(
            await context.Context(self.repository_ctxt, p3).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge low (queue)",
        )

        assert (
            check["output"]["title"]
            == "The pull request is the 2nd in the queue to be merged"
        )

    async def test_update_queue_freeze(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))

        queue_name = "default"
        freeze_payload = {"reason": "test freeze reason"}

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        queue_freeze_data_default = await freeze.QueueFreeze.get(
            self.repository_ctxt, "default"
        )

        assert queue_freeze_data_default is not None
        assert queue_freeze_data_default.reason == "test freeze reason"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        freeze_payload = {"reason": "new test freeze reason"}
        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": queue_name,
                    "reason": "new test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        freeze_payload = {"reason": ""}
        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": queue_name,
                    "reason": "No freeze reason was specified.",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

    async def test_request_error_delete_queue_freeze(self):
        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))

        queue_name = "false_queue_name"

        r = await self.app.delete(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 404
        assert r.json() == {
            "detail": f'The queue "{queue_name}" does not exist or is not currently frozen.'
        }

        queue_name = "default"

        r = await self.app.delete(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 404
        assert r.json() == {
            "detail": f'The queue "{queue_name}" does not exist or is not currently frozen.'
        }

    async def test_delete_queue_freeze(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))

        freeze_payload = {"reason": "test freeze reason"}
        queue_name = "default"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_payload,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        queue_freeze_data_default = await freeze.QueueFreeze.get(
            self.repository_ctxt, "default"
        )

        assert queue_freeze_data_default
        assert queue_freeze_data_default.reason == "test freeze reason"

        p1 = await self.create_pr()
        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge default (queue)",
        )

        assert (
            check["output"]["title"]
            == 'The queue "default" is currently frozen, for the following reason: test freeze reason'
        )

        r = await self.app.delete(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 204

        queue_freeze_data_default = await freeze.QueueFreeze.get(
            self.repository_ctxt, "default"
        )

        assert queue_freeze_data_default is None

        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge default (queue)",
        )

        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

    async def test_request_error_get_queue_freeze(self):
        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))
        queue_name = "false_queue_name"

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 404
        assert r.json() == {
            "detail": f'The queue "{queue_name}" does not exist or is not currently frozen.'
        }

        queue_name = "urgent"

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 404
        assert r.json() == {
            "detail": f'The queue "{queue_name}" does not exist or is not currently frozen.'
        }

    async def test_get_queue_freeze(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))

        freeze_json = {"reason": "test freeze reason"}
        queue_name = "default"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_json,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        queue_freeze_data_default = await freeze.QueueFreeze.get(
            self.repository_ctxt, "default"
        )

        assert queue_freeze_data_default
        assert queue_freeze_data_default.reason == "test freeze reason"

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

    async def test_get_list_queue_freeze(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [
                        "status-success=continuous-integration/fast-ci",
                    ],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                },
                {
                    "name": "low-priority",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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

        await self.setup_repo(yaml.dump(rules))

        freeze_json = {"reason": "test freeze reason"}
        queue_name = "default"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_json,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "default",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        queue_name = "low-priority"

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            json=freeze_json,
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "freeze_date": mock.ANY,
                    "name": "low-priority",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                }
            ],
        }

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/freezes",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        json_data = r.json()
        list_queue_name = [qf["name"] for qf in json_data["queue_freezes"]]
        assert "default" in list_queue_name
        assert "low-priority" in list_queue_name

        queue_name = "default"

        r = await self.app.delete(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 204

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/freezes",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [
                {
                    "name": "low-priority",
                    "reason": "test freeze reason",
                    "application_name": "testing application",
                    "application_id": 123,
                    "freeze_date": mock.ANY,
                },
            ],
        }

        queue_name = "low-priority"

        r = await self.app.delete(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/{queue_name}/freeze",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 204

        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues/freezes",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )

        assert r.status_code == 200
        assert r.json() == {
            "queue_freezes": [],
        }

    async def test_queue_freeze_with_pr_just_about_to_merge(self):

        rules = {
            "queue_rules": [
                {
                    "name": "urgent",
                    "conditions": [],
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
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
            ],
        }

        await self.setup_repo(yaml.dump(rules))

        r = await self.app.put(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queue/default/freeze",
            json={"reason": "test freeze reason"},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        # Everything is ready, but queue is frozen
        await self.create_status(p1, context="continuous-integration/slow-ci")
        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge default (queue)",
        )

        assert (
            check["output"]["title"]
            == 'The queue "default" is currently frozen, for the following reason: test freeze reason'
        )

        # ensure p2 got queued before p1 and merged
        await self.add_label(p2["number"], "queue-urgent")
        await self.run_engine()
        p2 = await self.get_pull(p2["number"])
        assert p2["merged"]
