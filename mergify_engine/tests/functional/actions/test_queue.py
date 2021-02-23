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
import typing

from first import first
import pytest
import yaml

from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine.queue import merge_train
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TrainCarMatcher(typing.NamedTuple):
    user_pull_request_number: github_types.GitHubPullRequestNumber
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    current_base_sha: github_types.SHAType
    state: merge_train.TrainCarState
    queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]


class TestQueueAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    @staticmethod
    def _assert_car(car: merge_train.TrainCar, expected_car: TrainCarMatcher) -> None:
        assert car.user_pull_request_number == expected_car.user_pull_request_number
        assert (
            car.parent_pull_request_numbers == expected_car.parent_pull_request_numbers
        )
        assert car.initial_current_base_sha == expected_car.initial_current_base_sha
        assert car.current_base_sha == expected_car.current_base_sha
        assert car.state == expected_car.state
        assert car.queue_pull_request_number == expected_car.queue_pull_request_number

    @classmethod
    async def _assert_cars_contents(
        cls, q: merge_train.Train, expected_contents: typing.List[TrainCarMatcher]
    ) -> None:
        await q.load()
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p.user_pull_request_number for p in expected_contents]
        contents = [
            c for c in q._iter_pseudo_cars() if isinstance(c, merge_train.TrainCar)
        ]
        assert len(contents) == len(expected_contents)
        for i, expected_car in enumerate(expected_contents):
            cls._assert_car(contents[i], expected_car)

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
        p2, _ = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p.update()

        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_pull = pulls[0]
        assert tmp_pull.number not in [p1.number, p2.number]

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_pull.number,
                ),
            ],
        )

        # TODO(sileht): Add some assertion on check-runs content

        assert tmp_pull.commits == 5
        await self.create_status(tmp_pull)

        head_sha = p1.head.sha
        p1.update()
        assert p1.head.sha != head_sha  # ensure it have been rebased
        await self.create_status(p1)

        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

    async def test_merge_queue_refresh(self):
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
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p1.raw_data)
        q = await merge_train.Train.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]

        mq_pr = self.r_o_integration.get_pull(q._cars[1].queue_pull_request_number)

        mq_pr.create_issue_comment("@mergifyio update")
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        mq_pr.update()
        comments = list(mq_pr.get_issue_comments())
        assert "Command not allowed on merge queue pull request." == comments[-1].body

        mq_pr.create_issue_comment("@mergifyio refresh")
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        mq_pr.update()
        comments = list(mq_pr.get_issue_comments())
        assert (
            "**Command `refresh`: success**\n> **Pull request refreshed**\n> \n"
            == comments[-1].body
        )

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
        await self.run_engine()
        p.update()

        # Queue PRs
        await self.add_label(p1, "queue")
        await self.run_engine()
        await self.add_label(p2, "queue")
        await self.run_engine()

        # Check Queue
        pulls = list(self.r_o_admin.get_pulls())
        # 1 queued and rebased PR + 1 queue PR with its tmp PR + 1 one not queued PR
        assert len(pulls) == 4

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2.number not in [p1.number, p2.number, p3.number]

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )

        # ensure it have been rebased
        head_sha = p1.head.sha
        p1.update()
        assert p1.head.sha != head_sha

        # Merge p1
        await self.create_status(p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3
        p1.update()
        assert p1.merged

        # ensure base is p, it's tested with p1, but current_base_sha have changed since
        # we create the tmp pull request
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )

        # Queue p3
        await self.add_label(p3, "queue")
        await self.run_engine()

        # Check train state
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3.number not in [
            p1.number,
            p2.number,
            p3.number,
            tmp_mq_p2.number,
        ]

        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                # Ensure p2 car is still the same
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
                # Ensure base is p1 and only p2 is tested with p3
                TrainCarMatcher(
                    p3.number,
                    [p2.number],
                    p1.merge_commit_sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p3.number,
                ),
            ],
        )

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
        p.update()

        # Queue two pulls
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2.number not in [p1.number, p2.number]

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )

        # p2 is ready first, ensure it's not merged
        await self.create_status(tmp_mq_p2)
        await self.run_engine()
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 2

        # Nothing change
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )
        # TODO(sileht): look state of p2 merge queue check-run

        # p1 is ready, check both are merged in a row
        p1.update()
        await self.create_status(p1)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0
        await self._assert_cars_contents(q, [])
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
        p.update()

        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2.number not in [p1.number, p2.number]

        ctxt = context.Context(self.repository_ctxt, p.raw_data)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )

        # tmp merge-queue pull p2 fail
        await self.create_status(tmp_mq_p2, state="failure")
        await self.run_engine()

        # then p1 fail too
        p1.update()
        await self.create_status(p1, state="failure")
        await self.run_engine()

        # TODO(sileht): Add some assertion on check-runs content

        # tmp merge-queue pull p2 have been closed and p2 updated/rebased
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 2
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
            ],
        )

        # Merge p2
        p2.update()
        await self.create_status(p2)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine()

        # Only p1 is still there and the queue is empty
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 1
        assert pulls[0].number == p1.number
        await self._assert_cars_contents(q, [])

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

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 4

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3.number not in [p1.number, p2.number, p3.number]

        # Check only p1 and p3 are in the train
        ctxt_p1 = context.Context(self.repository_ctxt, p1.raw_data)
        q = await merge_train.Train.from_context(ctxt_p1)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p1.base.sha,
                    p1.base.sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p3.number,
                    [p1.number],
                    p1.base.sha,
                    p1.base.sha,
                    "created",
                    tmp_mq_p3.number,
                ),
            ],
        )

        # Ensure p2 status is updated with the failure
        p2.update()
        ctxt_p2 = context.Context(self.repository_ctxt, p2.raw_data)
        check = first(
            await ctxt_p2.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert (
            check["output"]["title"] == "This pull request cannot be embarked for merge"
        )
        assert (
            check["output"]["summary"]
            == "The merge-queue pull request can't be created\nDetails: `Merge conflict`"
        )

        # Merge the train
        await self.create_status(p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.create_status(tmp_mq_p3)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})

        # Only p2 is remaining and not in train
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 1
        assert pulls[0].number == p2.number
        await self._assert_cars_contents(q, [])

    async def test_queue_cancel_and_refresh(self):
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
                    "name": "Tchou tchou",
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
        p3, _ = await self.create_pr()

        # Queue PRs
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")
        await self.add_label(p3, "queue")
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 5

        tmp_mq_p3 = pulls[0]
        tmp_mq_p2 = pulls[1]
        assert tmp_mq_p3.number not in [p1.number, p2.number, p3.number]
        assert tmp_mq_p2.number not in [p1.number, p2.number, p3.number]

        ctxt_p_merged = context.Context(self.repository_ctxt, p1.raw_data)
        q = await merge_train.Train.from_context(ctxt_p_merged)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p1.base.sha,
                    p1.base.sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p1.base.sha,
                    p1.base.sha,
                    "created",
                    tmp_mq_p2.number,
                ),
                TrainCarMatcher(
                    p3.number,
                    [p1.number, p2.number],
                    p1.base.sha,
                    p1.base.sha,
                    "created",
                    tmp_mq_p3.number,
                ),
            ],
        )

        await self.create_status(p1)
        await self.run_engine()

        # Ensure p1 is removed and current_head_sha have been updated on p2 and p3
        p1.update()
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p1.base.sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
                TrainCarMatcher(
                    p3.number,
                    [p1.number, p2.number],
                    p1.base.sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p3.number,
                ),
            ],
        )

        # tmp merge-queue pr p2, CI fails
        await self.create_status(tmp_mq_p2, state="failure")
        await self.run_engine()

        # tmp merge-queue pr p2 and p3 have been closed
        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 2

        # p3 is now rebased instead of havin a tmp merge-queue pr
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p3.number,
                    [],
                    p1.merge_commit_sha,
                    p1.merge_commit_sha,
                    "updated",
                    None,
                ),
            ],
        )

        # refresh to add it back in queue
        ctxt = await self.repository_ctxt.get_pull_request_context(
            p2.number, p2.raw_data
        )
        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        check_suite_id = check["check_suite"]["id"]

        # click on refresh btn
        await self.installation_ctxt.client.post(
            f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}/rerequest"
        )
        await self.wait_for("check_suite", {"action": "rerequested"})
        await self.run_engine()

        # Check pull is back to the queue and tmp pull recreated

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p2_bis.number not in [p1.number, p2.number, p3.number]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p3.number,
                    [],
                    p1.merge_commit_sha,
                    p1.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p3.number],
                    p1.merge_commit_sha,
                    p1.merge_commit_sha,
                    "created",
                    tmp_mq_p2_bis.number,
                ),
            ],
        )

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
        p, _ = await self.create_pr()
        p.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p.update()

        # Queue PRs
        await self.add_label(p1, "queue")
        await self.add_label(p2, "queue")

        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2.number not in [p1.number, p2.number]

        ctxt_p_merged = context.Context(self.repository_ctxt, p.raw_data)
        q = await merge_train.Train.from_context(ctxt_p_merged)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p.merge_commit_sha,
                    p.merge_commit_sha,
                    "created",
                    tmp_mq_p2.number,
                ),
            ],
        )

        # Ensure p1 have been rebased
        head_sha = p1.head.sha
        p1.update()
        assert p1.head.sha != head_sha

        # Merge a not queued PR manually
        p_merged_in_meantime, _ = await self.create_pr()
        p_merged_in_meantime.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        p_merged_in_meantime.update()

        await self.run_engine(3)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p2_bis.number not in [p1.number, p2.number]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p_merged_in_meantime.merge_commit_sha,
                    p_merged_in_meantime.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2.number,
                    [p1.number],
                    p_merged_in_meantime.merge_commit_sha,
                    p_merged_in_meantime.merge_commit_sha,
                    "created",
                    tmp_mq_p2_bis.number,
                ),
            ],
        )

        # Check train have been reseted on top on the new sha one
        # Ensure p1 have been rebased again and p2 got recreate with more commits
        head_sha = p1.head.sha
        p1.update()
        assert p1.head.sha != head_sha
        assert tmp_mq_p2_bis.commits == 5

        # Merge the train
        await self.create_status(p1)
        await self.create_status(tmp_mq_p2_bis)
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

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
        p_merged, _ = await self.create_pr()
        p_merged.merge()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p_merged.update()

        # Put first PR in queue
        await self.add_label(p1, "queue")
        await self.run_engine()

        ctxt_p_merged = context.Context(self.repository_ctxt, p_merged.raw_data)
        q = await merge_train.Train.from_context(ctxt_p_merged)

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 2

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1.number,
                    [],
                    p_merged.merge_commit_sha,
                    p_merged.merge_commit_sha,
                    "updated",
                    None,
                ),
            ],
        )

        # ensure it have been rebased
        head_sha = p1.head.sha
        p1.update()
        assert p1.head.sha != head_sha
        assert p1.commits == 2

        # Put second PR at the begining of the queue via queue priority
        await self.add_label(p2, "queue-urgent")
        await self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        assert len(pulls) == 3

        tmp_mq_p1 = pulls[0]
        assert tmp_mq_p1.number not in [p1.number, p2.number]

        # p2 insert at the begining
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2.number,
                    [],
                    p_merged.merge_commit_sha,
                    p_merged.merge_commit_sha,
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p1.number,
                    [p2.number],
                    p_merged.merge_commit_sha,
                    p_merged.merge_commit_sha,
                    "created",
                    tmp_mq_p1.number,
                ),
            ],
        )

        # ensure it have been rebased and tmp merge-queue pr of p1 have all commits
        head_sha = p2.head.sha
        p2.update()
        assert p2.head.sha != head_sha
        assert tmp_mq_p1.commits == 5

    async def test_queue_no_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge train",
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
        await self.create_status(p1)
        await self.add_label(p1, "queue")
        await self.run_engine()

        ctxt_p1 = context.Context(self.repository_ctxt, p1.raw_data)
        q = await merge_train.Train.from_context(ctxt_p1)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == []

        # pull merged without need of a train car
        p1.update()
        assert p1.merged


class TestTrainApiCalls(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_create_pull_basic(self):
        await self.setup_repo(yaml.dump({}))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        ctxt = context.Context(self.repository_ctxt, p1.raw_data)
        q = await merge_train.Train.from_context(ctxt)
        head_sha = await q.get_head_sha()

        config = queue.QueueConfig(
            name="foo",
            strict_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
        )

        car = merge_train.TrainCar(
            q,
            p2.number,
            [p1.number],
            config,
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
        q = await merge_train.Train.from_context(ctxt)
        head_sha = await q.get_head_sha()

        config = queue.QueueConfig(
            name="foo",
            strict_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
        )

        car = merge_train.TrainCar(
            q,
            p3.number,
            [p1.number, p2.number],
            config,
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
        assert (
            check["output"]["summary"]
            == "The merge-queue pull request can't be created\nDetails: `Merge conflict`"
        )
