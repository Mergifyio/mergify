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

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine.queue import merge_train
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)

TEMPLATE_GITHUB_ACTION = """
name: Continuous Integration
on:
  pull_request:
    branches:
      - master

jobs:
  unit-tests:
    timeout-minutes: 5
    runs-on: ubuntu-20.04
    steps:
      - uses: actions/checkout@v2
      - run: %s
"""


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
        cls,
        q: merge_train.Train,
        expected_cars: typing.List[TrainCarMatcher],
        expected_waiting_pulls: typing.Optional[
            typing.List[github_types.GitHubPullRequestNumber]
        ] = None,
    ) -> None:
        if expected_waiting_pulls is None:
            expected_waiting_pulls = []

        await q.load()
        pulls_in_queue = await q.get_pulls()
        assert (
            pulls_in_queue
            == [p.user_pull_request_number for p in expected_cars]
            + expected_waiting_pulls
        )

        contents = list(q._iter_pseudo_cars())
        assert len(contents) == len(expected_cars) + len(expected_waiting_pulls)
        for i, expected_car in enumerate(expected_cars):
            car = contents[i]
            assert isinstance(car, merge_train.TrainCar)
            cls._assert_car(car, expected_car)

        for i, expected_waiting_pull in enumerate(expected_waiting_pulls):
            wp = contents[len(expected_cars) + i]
            assert isinstance(wp, merge_train.WaitingPull)
            assert wp.user_pull_request_number == expected_waiting_pull

    async def test_basic_queue(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = await self.get_pull(pulls[0]["number"])
        assert tmp_pull["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull["number"],
                ),
            ],
        )

        assert tmp_pull["commits"] == 5
        await self.create_status(tmp_pull)

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        async def assert_queued():
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
            )
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )

        await assert_queued()

        await self.create_comment(p1["number"], "@mergifyio refresh")
        await self.run_engine()
        await assert_queued()

        await self.create_status(p1, state="pending")
        await self.run_engine()
        await assert_queued()

        await self.create_status(p1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

    async def test_queue_with_labels(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                        "label=foobar",
                    ],
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = await self.get_pull(pulls[0]["number"])
        assert tmp_pull["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull["number"],
                ),
            ],
        )

        assert tmp_pull["commits"] == 4
        await self.create_status(tmp_pull)

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        async def assert_queued(pull):
            check = first(
                await context.Context(
                    self.repository_ctxt, pull
                ).pull_engine_check_runs,
                key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
            )
            assert check["conclusion"] is None

        await assert_queued(p1)
        await assert_queued(p2)

        await self.create_status(p1, state="pending")
        await self.create_status(p2, state="pending")
        await self.run_engine()
        await assert_queued(p1)
        await assert_queued(p2)

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        await self.create_status(p1)
        await self.create_status(p2)
        await self.run_engine()
        await assert_queued(p1)
        await assert_queued(p2)

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        await self.add_label(p1["number"], "foobar")
        await self.add_label(p2["number"], "foobar")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

    async def test_queue_with_ci_in_pull_request_rules(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=queue",
                        "status-success=continuous-integration/fake-ci",
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.create_status(p1)
        await self.add_label(p1["number"], "queue")
        await self.create_status(p2)
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = await self.get_pull(pulls[0]["number"])
        assert tmp_pull["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull["number"],
                ),
            ],
        )

        # May or may not contains the merge commit of the first pr updated
        assert tmp_pull["commits"] in [5, 6]
        await self.create_status(tmp_pull)

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )
        await self.create_status(p1, state="pending")
        await self.run_engine()

        # Ensure it have not been cancelled on pending event
        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )
        await self.create_status(p1, state="success")
        await self.run_engine()

        pulls = await self.get_pulls()
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
                    "speculative_checks": 5,
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
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1["number"], p2["number"]]

        mq_pr_number = q._cars[1].queue_pull_request_number

        await self.create_comment(mq_pr_number, "@mergifyio update")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(mq_pr_number)
        assert (
            "Command not allowed on merge queue pull request." == comments[-1]["body"]
        )

        await self.create_comment(mq_pr_number, "@mergifyio refresh")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(mq_pr_number)
        assert (
            "**Command `refresh`: success**\n> **Pull request refreshed**\n> \n"
            == comments[-1]["body"]
        )

    async def test_ongoing_train_basic(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Queue PRs
        await self.add_label(p1["number"], "queue")
        await self.run_engine()
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        # Check Queue
        pulls = await self.get_pulls()
        # 1 queued and rebased PR + 1 queue PR with its tmp PR + 1 one not queued PR
        assert len(pulls) == 4

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # ensure it have been rebased
        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha

        # Merge p1
        await self.create_status(p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)
        pulls = await self.get_pulls()
        assert len(pulls) == 3
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]

        # ensure base is p, it's tested with p1, but current_base_sha have changed since
        # we create the tmp pull request
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Queue p3
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        # Check train state
        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3["number"] not in [
            p1["number"],
            p2["number"],
            p3["number"],
            tmp_mq_p2["number"],
        ]

        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                # Ensure p2 car is still the same
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                # Ensure base is p1 and only p2 is tested with p3
                TrainCarMatcher(
                    p3["number"],
                    [p2["number"]],
                    p1["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p3["number"],
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
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Queue two pulls
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # p2 is ready first, ensure it's not merged
        await self.create_status(tmp_mq_p2)
        await self.run_engine()
        pulls = await self.get_pulls()
        assert len(pulls) == 2

        # Nothing change
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )
        # TODO(sileht): look state of p2 merge queue check-run

        # p1 is ready, check both are merged in a row
        p1 = await self.get_pull(p1["number"])
        await self.create_status(p1)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine(3)

        pulls = await self.get_pulls()
        assert len(pulls) == 0
        await self._assert_cars_contents(q, [])
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
        p2 = await self.get_pull(p2["number"])
        assert p2["merged"]

    async def test_queue_ci_failure(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # tmp merge-queue pull p2 fail
        await self.create_status(tmp_mq_p2, state="failure")
        await self.run_engine()

        # then p1 fail too
        p1 = await self.get_pull(p1["number"])
        await self.create_status(p1, state="failure")
        await self.run_engine()

        # TODO(sileht): Add some assertion on check-runs content

        # tmp merge-queue pull p2 have been closed and p2 updated/rebased
        pulls = await self.get_pulls()
        assert len(pulls) == 2
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
            ],
        )

        # Merge p2
        p2 = await self.get_pull(p2["number"])
        await self.create_status(p2)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        await self.run_engine()

        # Only p1 is still there and the queue is empty
        pulls = await self.get_pulls()
        assert len(pulls) == 1
        assert pulls[0]["number"] == p1["number"]
        await self._assert_cars_contents(q, [])

    async def test_queue_cant_create_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine(3)

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3["number"] not in [p1["number"], p2["number"], p3["number"]]

        # Check only p1 and p3 are in the train
        ctxt_p1 = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt_p1)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p1["base"]["sha"],
                    p1["base"]["sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p3["number"],
                    [p1["number"]],
                    p1["base"]["sha"],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq_p3["number"],
                ),
            ],
        )

        # Ensure p2 status is updated with the failure
        p2 = await self.get_pull(p2["number"])
        ctxt_p2 = context.Context(self.repository_ctxt, p2)
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
        pulls = await self.get_pulls()
        assert len(pulls) == 1
        assert pulls[0]["number"] == p2["number"]
        await self._assert_cars_contents(q, [])

    async def test_queue_cancel_and_refresh(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_mq_p3 = pulls[0]
        tmp_mq_p2 = pulls[1]
        assert tmp_mq_p3["number"] not in [p1["number"], p2["number"], p3["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        ctxt_p_merged = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt_p_merged)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p1["base"]["sha"],
                    p1["base"]["sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p1["base"]["sha"],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                TrainCarMatcher(
                    p3["number"],
                    [p1["number"], p2["number"]],
                    p1["base"]["sha"],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq_p3["number"],
                ),
            ],
        )

        await self.create_status(p1)
        await self.run_engine()

        # Ensure p1 is removed and current["head"]["sha"] have been updated on p2 and p3
        p1 = await self.get_pull(p1["number"])
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p1["base"]["sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                TrainCarMatcher(
                    p3["number"],
                    [p1["number"], p2["number"]],
                    p1["base"]["sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p3["number"],
                ),
            ],
        )

        # tmp merge-queue pr p2, CI fails
        await self.create_status(tmp_mq_p2, state="failure")
        await self.run_engine()

        # tmp merge-queue pr p2 and p3 have been closed
        pulls = await self.get_pulls()
        assert len(pulls) == 2

        # p3 is now rebased instead of havin a tmp merge-queue pr
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p3["number"],
                    [],
                    p1["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "updated",
                    None,
                ),
            ],
        )

        # refresh to add it back in queue
        ctxt = await self.repository_ctxt.get_pull_request_context(p2["number"], p2)
        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        check_suite_id = check["check_suite"]["id"]

        # click on refresh btn
        await self.installation_ctxt.client.post(
            f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}/rerequest",
            api_version="antiope",
        )
        await self.wait_for("check_suite", {"action": "rerequested"})
        await self.run_engine()

        # Check pull is back to the queue and tmp pull recreated

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p2_bis["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p3["number"],
                    [],
                    p1["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p3["number"]],
                    p1["merge_commit_sha"],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p2_bis["number"],
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
                    "speculative_checks": 5,
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
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Queue PRs
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")

        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt_p_merged = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt_p_merged)
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Ensure p1 have been rebased
        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha

        # Merge a not queued PR manually
        p_merged_in_meantime, _ = await self.create_pr()
        await self.merge_pull(p_merged_in_meantime["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})
        p_merged_in_meantime = await self.get_pull(p_merged_in_meantime["number"])

        await self.run_engine(3)

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p2_bis = await self.get_pull(pulls[0]["number"])
        assert tmp_mq_p2_bis["number"] not in [p1["number"], p2["number"]]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p_merged_in_meantime["merge_commit_sha"],
                    p_merged_in_meantime["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p_merged_in_meantime["merge_commit_sha"],
                    p_merged_in_meantime["merge_commit_sha"],
                    "created",
                    tmp_mq_p2_bis["number"],
                ),
            ],
        )

        # Check train have been reseted on top on the new sha one
        # Ensure p1 have been rebased again and p2 got recreate with more commits
        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha
        assert tmp_mq_p2_bis["commits"] == 5

        # Merge the train
        await self.create_status(p1)
        await self.create_status(tmp_mq_p2_bis)
        await self.run_engine()

        pulls = await self.get_pulls()
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
                    "speculative_checks": 5,
                },
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "speculative_checks": 5,
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
        p3, _ = await self.create_pr()

        # To force others to be rebased
        p_merged, _ = await self.create_pr()
        await self.merge_pull(p_merged["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p_merged = await self.get_pull(p_merged["number"])

        # Put first PR in queue
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        ctxt_p_merged = context.Context(self.repository_ctxt, p_merged)
        q = await merge_train.Train.from_context(ctxt_p_merged)

        # my 3 PRs + 1 merge-queue PR
        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p_merged["merge_commit_sha"],
                    p_merged["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p_merged["merge_commit_sha"],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
            ],
        )

        # ensure it have been rebased
        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha
        assert p1["commits"] == 2

        # Put second PR at the begining of the queue via queue priority
        await self.add_label(p3["number"], "queue-urgent")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        # p3 is now the only car in train, as its queue is not the same as p1 and p2
        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p3["number"],
                    [],
                    p_merged["merge_commit_sha"],
                    p_merged["merge_commit_sha"],
                    "updated",
                    None,
                ),
            ],
            [p1["number"], p2["number"]],
        )

        r = await self.app.get(
            f"/queues/{config.TESTING_ORGANIZATION_ID}",
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )

        assert r.json() == {
            f"{self.REPO_ID}": {
                self.master_branch_name: [p3["number"], p1["number"], p2["number"]]
            }
        }

        # ensure it have been rebased and tmp merge-queue pr of p1 have all commits
        head_sha = p3["head"]["sha"]
        p3 = await self.get_pull(p3["number"])
        assert p3["head"]["sha"] != head_sha

        # Merge p3
        await self.create_status(p3, context="continuous-integration/fast-ci")
        await self.run_engine()
        p3 = await self.get_pull(p3["number"])
        assert p3["merged"]

        # ensure p1 and p2 are back in queue
        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_mq_p1 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p3["merge_commit_sha"],
                    p3["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p3["merge_commit_sha"],
                    p3["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
            ],
        )

        # ensure it have been rebased
        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha
        assert p1["commits"] == 3

    async def test_queue_no_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        ctxt_p1 = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt_p1)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == []

        # pull merged without need of a train car
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]

    # FIXME(sileht): Provide a tools to generate oauth_token without
    # the need of the dashboard
    @pytest.mark.skipif(
        config.GITHUB_URL != "https://github.com",
        reason="We use a PAT token instead of an OAUTH_TOKEN",
    )
    async def test_pull_have_base_branch_merged_commit_with_changed_workflow(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
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
        await self.setup_repo(
            yaml.dump(rules),
            files={
                ".github/workflows/ci.yml": TEMPLATE_GITHUB_ACTION % "echo Default CI"
            },
        )

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p, _ = await self.create_pr(
            files={
                ".github/workflows/ci.yml": TEMPLATE_GITHUB_ACTION % "echo Changed CI"
            }
        )

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Merge base branch into p2
        await self.client_admin.put(
            f"{self.url_main}/pulls/{p2['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": p2["head"]["sha"]},
        )

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = await self.get_pull(pulls[0]["number"])
        assert tmp_pull["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
                TrainCarMatcher(
                    p2["number"],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull["number"],
                ),
            ],
        )

        assert tmp_pull["commits"] == 7
        await self.create_status(tmp_pull)

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_status(p1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

    async def test_more_ci_in_pull_request_rules_succeed(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "status-success=very-long-ci",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.create_status(p1)
        await self.create_status(p1, context="very-long-ci")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
            ],
        )

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_status(p1)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.master_branch_name}"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, [])

    async def test_more_ci_in_pull_request_rules_failure(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "status-success=very-long-ci",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.create_status(p1)
        await self.create_status(p1, context="very-long-ci")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            [
                TrainCarMatcher(
                    p1["number"],
                    [],
                    p["merge_commit_sha"],
                    p["merge_commit_sha"],
                    "updated",
                    None,
                ),
            ],
        )

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased

        await self.run_engine()
        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.remove_label(p1["number"], "queue")
        await self.run_engine()

        # not merged and unqueued
        pulls = await self.get_pulls()
        assert len(pulls) == 1

        await self._assert_cars_contents(q, [])


class TestTrainApiCalls(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_create_pull_basic(self):
        await self.setup_repo(yaml.dump({}))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        head_sha = await q.get_head_sha()

        queue_config = rules.QueueConfig(priority=0, speculative_checks=5)
        config = queue.PullQueueConfig(
            name="foo",
            strict_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
            queue_config=queue_config,
        )

        car = merge_train.TrainCar(
            q,
            p2["number"],
            [p1["number"]],
            config,
            head_sha,
            head_sha,
            date.utcnow(),
        )
        await car.create_pull(
            rules.QueueRule(
                name="foo", conditions=rules.RuleConditions([]), config=queue_config
            )
        )
        assert car.queue_pull_request_number is not None
        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = [p for p in pulls if p["number"] == car.queue_pull_request_number][0]
        assert tmp_pull["draft"]

        await car.delete_pull()

        # NOTE(sileht): When branch is deleted the associated Pull is deleted in an async
        # fashion on GitHub side.
        time.sleep(1)
        pulls = await self.get_pulls()
        assert len(pulls) == 2

    async def test_create_pull_conflicts(self):
        await self.setup_repo(yaml.dump({}), files={"conflicts": "foobar"})

        p, _ = await self.create_pr(files={"conflicts": "well"})
        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
        p3, _ = await self.create_pr(files={"conflicts": "boom"})

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        head_sha = await q.get_head_sha()

        queue_config = rules.QueueConfig(priority=0, speculative_checks=5)
        config = queue.PullQueueConfig(
            name="foo",
            strict_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
            queue_config=queue_config,
        )

        car = merge_train.TrainCar(
            q,
            p3["number"],
            [p1["number"], p2["number"]],
            config,
            head_sha,
            head_sha,
            date.utcnow(),
        )
        with pytest.raises(merge_train.TrainCarPullRequestCreationFailure) as exc_info:
            await car.create_pull(
                rules.QueueRule(
                    name="foo", conditions=rules.RuleConditions([]), config=queue_config
                )
            )
            assert exc_info.value.car == car
            assert car.queue_pull_request_number is None

        p3 = await self.get_pull(p3["number"])
        ctxt_p3 = context.Context(self.repository_ctxt, p3)
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
