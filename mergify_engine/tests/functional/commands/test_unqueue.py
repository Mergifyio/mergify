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

from first import first
import yaml

from mergify_engine import constants
from mergify_engine import context
from mergify_engine.queue import merge_train
from mergify_engine.tests.functional import base
from mergify_engine.tests.functional.actions import test_queue


class TestUnQueueCommand(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_unqueue(self) -> None:
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Queue",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        await self.run_engine()
        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        base_sha = await q.get_base_sha()
        await test_queue.TestQueueAction._assert_cars_contents(
            q,
            base_sha,
            [
                test_queue.TrainCarMatcher(
                    [p1["number"]],
                    [],
                    base_sha,
                    "updated",
                    p1["number"],
                ),
            ],
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Queue (queue)",
        )
        assert check is not None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_comment_as_admin(p1["number"], "@mergifyio requeue")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p1["number"])
        assert (
            comments[-1]["body"]
            == """> requeue

#### ☑️ This pull request is already queued



<!--
DO NOT EDIT
-*- Mergify Payload -*-
{"command": "requeue", "conclusion": "neutral"}
-*- Mergify Payload End -*-
-->
"""
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Queue (queue)",
        )
        assert check is not None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_comment_as_admin(p1["number"], "@mergifyio unqueue")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        await test_queue.TestQueueAction._assert_cars_contents(q, None, [])

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Queue (queue)",
        )
        assert check is not None
        assert check["conclusion"] == "cancelled"
        assert (
            check["output"]["title"]
            == "The pull request has been removed from the queue"
        )
        assert (
            check["output"]["summary"]
            == "The pull request has been manually removed from the queue by an `unqueue` command."
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert check is not None
        assert check["conclusion"] == "cancelled"
        assert (
            check["output"]["title"]
            == "The pull request has been removed from the queue by an `unqueue` command"
        )

        await self.create_comment_as_admin(p1["number"], "@mergifyio requeue")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert check is not None
        assert check["conclusion"] is None
        assert check["output"]["title"].startswith("The pull request is embarked with")

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Queue (queue)",
        )
        assert check is not None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )
        await self.create_status(p1)
        await self.run_engine()

        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
