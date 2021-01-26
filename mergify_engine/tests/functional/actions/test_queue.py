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
import logging
import time

from first import first
import pytest
import yaml

from mergify_engine import constants
from mergify_engine import context
from mergify_engine import merge_train
from mergify_engine.actions.merge import queue
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestQueueAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_basic_queue(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]
        assert q.train._cars[0].user_pull_request_number == p1.number
        assert q.train._cars[1].user_pull_request_number == p2.number

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        # TODO(sileht): Add some assertion on check-runs content

        for pull in pulls:
            if pull.number not in [p1.number, p2.number]:
                await self.create_status(pull)

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0

        pulls_in_queue = await q.get_pulls()
        assert len(pulls_in_queue) == 0

    async def test_ongoing_train_basic(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
        p3, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        p.update()
        await self.run_engine()

        # Queue PRs
        await self.add_label(p1, "queue")
        await self.run_engine()
        await self.add_label(p2, "queue")
        await self.run_engine()

        # Check Queue
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 5
        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]

        assert q.train._cars[0].user_pull_request_number == p1.number
        assert q.train._cars[0].initial_current_base_sha == p.merge_commit_sha
        assert q.train._cars[0].current_base_sha == p.merge_commit_sha
        assert q.train._cars[0].parent_pull_request_numbers == []
        assert q.train._cars[1].user_pull_request_number == p2.number
        assert q.train._cars[1].initial_current_base_sha == p.merge_commit_sha
        assert q.train._cars[1].current_base_sha == p.merge_commit_sha
        assert q.train._cars[1].parent_pull_request_numbers == [p1.number]

        # Merge p1
        tmp_p1 = first(
            pulls, key=lambda p: p.number == q.train._cars[0].queue_pull_request_number
        )
        await self.create_status(tmp_p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p2.number]
        p1.update()
        assert p1.merged

        # ensure base is p, it's tested with p1, but current_base_sha have changed since
        # we create the tmp pull request
        assert q.train._cars[0].user_pull_request_number == p2.number
        assert q.train._cars[0].initial_current_base_sha == p.merge_commit_sha
        assert q.train._cars[0].current_base_sha == p1.merge_commit_sha
        assert q.train._cars[0].parent_pull_request_numbers == [p1.number]

        # Queue p3
        await self.add_label(p3, "queue")
        await self.run_engine()

        # Check train state
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p2.number, p3.number]

        # ensure base is p, it's tested with p1, but current_base_sha have changed since
        # we create the tmp pull request
        assert q.train._cars[0].user_pull_request_number == p2.number
        assert q.train._cars[0].initial_current_base_sha == p.merge_commit_sha
        assert q.train._cars[0].current_base_sha == p1.merge_commit_sha
        assert q.train._cars[0].parent_pull_request_numbers == [p1.number]

        # Ensure base is p1 and only p2 is tested with p3
        assert q.train._cars[1].user_pull_request_number == p3.number
        assert q.train._cars[1].initial_current_base_sha == p1.merge_commit_sha
        assert q.train._cars[1].current_base_sha == p1.merge_commit_sha
        assert q.train._cars[1].parent_pull_request_numbers == [p2.number]

    async def test_ongoing_train_second_pr_ready_first(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        # Queue two pulls
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]
        assert q.train._cars[0].user_pull_request_number == p1.number
        assert q.train._cars[1].user_pull_request_number == p2.number

        # Create all temporary PRs for merge queue
        await self.run_engine(3)
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        # p2 is ready first, ensure it's not merged
        tmp_p2 = first(
            pulls, key=lambda p: p.number == q.train._cars[1].queue_pull_request_number
        )
        await self.create_status(tmp_p2)
        await self.run_engine()
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]
        assert q.train._cars[0].user_pull_request_number == p1.number
        assert q.train._cars[1].user_pull_request_number == p2.number

        # TODO(sileht): look state of p2 merge queue check-run

        # p1 is ready, check both are merged in a row
        tmp_p1 = first(
            pulls, key=lambda p: p.number == q.train._cars[0].queue_pull_request_number
        )
        await self.create_status(tmp_p1)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == []
        p1.update()
        assert p1.merged
        p2.update()
        assert p2.merged

    async def test_queue_ci_failure(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]
        assert q.train._cars[0].user_pull_request_number == p1.number
        assert q.train._cars[1].user_pull_request_number == p2.number

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        tmp_p1 = first(
            pulls, key=lambda p: p.number == q.train._cars[0].queue_pull_request_number
        )
        await self.create_status(tmp_p1, state="failure")
        await self.run_engine()

        # TODO(sileht): Add some assertion on check-runs content

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3
        q = await queue.Queue.from_context(ctxt, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p2.number]
        assert q.train._cars[0].user_pull_request_number == p2.number

        # Merge p2
        tmp_p2 = first(
            pulls, key=lambda p: p.number == q.train._cars[0].queue_pull_request_number
        )
        await self.create_status(tmp_p2)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)

        # Only p1 is still there
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 1
        assert pulls[0].number == p1.number

    async def test_queue_cant_create_tmp_pull_request(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p1, _ = await self.create_pr(files={"conflicts": "well"})
        p2, _ = await self.create_pr(files={"conflicts": "boom"})
        p3, _ = await self.create_pr()

        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.add_label(p3, "queue")
        await self.run_engine(3)

        # Check everything is in queue and train
        ctxt_p1 = context.Context(self.repository_ctxt, p1.raw_data)
        q = await queue.Queue.from_context(ctxt_p1, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p3.number]

        # But only 2 instead temporary PRs have been created instead of 3
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 5

        p2.update()
        ctxt_p2 = context.Context(self.repository_ctxt, p2.raw_data)
        check = first(
            await ctxt_p2.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert (
            check["output"]["title"] == "This pull request cannot be embarked for merge"
        )
        assert check["output"]["summary"] == "Merge conflict"

        # Merge the train
        for pull in pulls:
            if pull.number not in [p1.number, p2.number, p3.number]:
                await self.create_status(pull)

        await self.run_engine(3)

        # Only p2 is remaining
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 1
        assert pulls[0].number == p2.number

        q = await queue.Queue.from_context(ctxt_p1, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert len(pulls_in_queue) == 0
        assert len(q.train._cars) == 0

    async def test_queue_manual_merge(self):
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
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p3, _ = await self.create_pr()
        p3.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        p3.update()
        await self.run_engine()

        # Queue PRs
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")

        await self.run_engine()

        ctxt_p3 = context.Context(self.repository_ctxt, p3.raw_data)
        q = await queue.Queue.from_context(ctxt_p3, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        # Check train is correct
        p3.update()
        tmp_pulls = []
        for pull in pulls:
            if pull.number not in [p1.number, p2.number]:
                tmp_pulls.append(pull.number)
                assert p3.merge_commit_sha == pull.base.sha

        # Merge a not queued PR manually
        p4, _ = await self.create_pr()
        p4.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        p4.update()

        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        # Check train have been reseted on top on the new sha one
        expected_commits = [2, 4]
        for pull in pulls:
            # Ensure previous tmp pulls have been closed
            assert pull.number is not tmp_pulls
            # Ensure new one have the correct base commit
            if pull.number not in [p1.number, p2.number]:
                assert p4.merge_commit_sha == pull.base.sha
                expected_commits.remove(pull.commits)
        assert expected_commits == []

        # Merge the train
        for pull in pulls:
            if pull.number not in [p1.number, p2.number]:
                await self.create_status(pull)

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0

        pulls_in_queue = await q.get_pulls()
        assert len(pulls_in_queue) == 0

    async def test_queue_priority(self):
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
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue-urgent",
                    ],
                    "actions": {"queue": {"name": "urgent"}},
                },
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p3, _ = await self.create_pr()
        p3.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p3.update()

        # Put first PR in queue
        await self.add_label(p1, "queue")
        await self.run_engine()

        ctxt_p3 = context.Context(self.repository_ctxt, p3.raw_data)
        q = await queue.Queue.from_context(ctxt_p3, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number]

        # Check train have been created
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_pulls = []
        for pull in pulls:
            if pull.number not in [p1.number, p2.number]:
                tmp_pulls.append(pull.number)
                assert p3.merge_commit_sha == pull.base.sha
                assert pull.commits == 2

        # Put second PR at the begining of the queue via queue priority
        await self.add_label(p2, "queue-urgent")
        await self.run_engine()

        ctxt_p3 = context.Context(self.repository_ctxt, p3.raw_data)
        q = await queue.Queue.from_context(ctxt_p3, with_train=True)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p2.number, p1.number]

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        # Check train have been reseted
        expected_commits = [2, 4]
        for pull in pulls:
            # Ensure previous tmp pulls have been closed
            assert pull.number is not tmp_pulls

            # Ensure the new one are correct
            if pull.number not in [p1.number, p2.number]:
                assert p3.merge_commit_sha == pull.base.sha
                expected_commits.remove(pull.commits)

        assert expected_commits == []


class TestTrainApiCalls(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_create_pull_basic(self):
        await self.setup_repo(yaml.dump({}))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        ctxt = context.Context(self.repository_ctxt, p1.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        head_sha = await q.train.get_head_sha()

        car = merge_train.TrainCar(
            q.train,
            p2.number,
            "whatever",
            [p1.number],
            head_sha,
            head_sha,
        )
        await car.create_pull()
        assert car.queue_pull_request_number is not None
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        await car.delete_pull()

        # NOTE(sileht): When branch is deleted the associated Pull is deleted in an async
        # fashion on GitHub side.
        time.sleep(1)
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 2

    async def test_create_pull_conflicts(self):
        await self.setup_repo(yaml.dump({}), files={"conflicts": "foobar"})

        p, _ = await self.create_pr(files={"conflicts": "well"})
        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
        p3, _ = await self.create_pr(files={"conflicts": "boom"})

        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await queue.Queue.from_context(ctxt, with_train=True)
        head_sha = await q.train.get_head_sha()

        car = merge_train.TrainCar(
            q.train,
            p3.number,
            "whatever",
            [p1.number, p2.number],
            head_sha,
            head_sha,
        )
        with pytest.raises(merge_train.TrainCarPullRequestCreationFailure) as exc_info:
            await car.create_pull()
            assert exc_info.value.car == car
            assert car.queue_pull_request_number is None

        p3.update()
        ctxt_p3 = context.Context(self.repository_ctxt, p3.raw_data)
        check = first(
            await ctxt_p3.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert (
            check["output"]["title"] == "This pull request cannot be embarked for merge"
        )
        assert check["output"]["summary"] == "Merge conflict"
