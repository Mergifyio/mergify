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
import copy
import datetime
import itertools
import operator
import typing
from unittest import mock

from first import first
from freezegun import freeze_time
import pytest
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine.queue import merge_train
from mergify_engine.rules import conditions
from mergify_engine.tests.functional import base


TEMPLATE_GITHUB_ACTION = """
name: Continuous Integration
on:
  pull_request:
    branches:
      - main

jobs:
  unit-tests:
    timeout-minutes: 5
    runs-on: ubuntu-20.04
    steps:
      - uses: actions/checkout@v2
      - run: %s
"""


class TrainCarMatcher(typing.NamedTuple):
    user_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    creation_state: merge_train.TrainCarState
    queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]


class TestQueueAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    @staticmethod
    def _assert_car(car: merge_train.TrainCar, expected_car: TrainCarMatcher) -> None:
        for i, ep in enumerate(car.still_queued_embarked_pulls):
            assert (
                ep.user_pull_request_number == expected_car.user_pull_request_numbers[i]
            )
        assert (
            car.parent_pull_request_numbers == expected_car.parent_pull_request_numbers
        )
        assert car.initial_current_base_sha == expected_car.initial_current_base_sha
        assert car.creation_state == expected_car.creation_state
        assert car.queue_pull_request_number == expected_car.queue_pull_request_number

    @classmethod
    async def _assert_cars_contents(
        cls,
        q: merge_train.Train,
        expected_base_sha: typing.Optional[github_types.SHAType],
        expected_cars: typing.List[TrainCarMatcher],
        expected_waiting_pulls: typing.Optional[
            typing.List[github_types.GitHubPullRequestNumber]
        ] = None,
    ) -> None:
        if expected_waiting_pulls is None:
            expected_waiting_pulls = []

        await q.load()
        assert q._current_base_sha == expected_base_sha

        pulls_in_queue = await q.get_pulls()
        assert (
            pulls_in_queue
            == list(
                itertools.chain.from_iterable(
                    [p.user_pull_request_numbers for p in expected_cars]
                )
            )
            + expected_waiting_pulls
        )

        assert len(q._cars) == len(expected_cars)
        for i, expected_car in enumerate(expected_cars):
            car = q._cars[i]
            cls._assert_car(car, expected_car)

        assert len(q._waiting_pulls) == len(expected_waiting_pulls)
        for i, expected_waiting_pull in enumerate(expected_waiting_pulls):
            wp = q._waiting_pulls[i]
            assert wp.user_pull_request_number == expected_waiting_pull

    async def test_queue_rule_deleted(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": True,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge me",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.run_engine()
        pulls = await self.get_pulls()
        assert len(pulls) == 1

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        assert len(await q.get_pulls()) == 1

        check = first(
            await context.Context(self.repository_ctxt, p).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge me (queue)",
        )
        assert check["conclusion"] is None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        updated_rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge only if label is present",
                    "conditions": [f"base={self.main_branch_name}", "label=automerge"],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }

        p2 = await self.create_pr(files={".mergify.yml": yaml.dump(updated_rules)})
        await self.merge_pull(p2["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        p = await self.get_pull(p["number"])
        check = first(
            await context.Context(self.repository_ctxt, p).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge me (queue)",
        )
        assert check["conclusion"] == "cancelled"
        assert check["output"]["title"] == "The rule doesn't match anymore"
        q = await merge_train.Train.from_context(ctxt)
        assert len(await q.get_pulls()) == 0

    async def test_queue_inplace_interrupted(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [f"base={self.main_branch_name}", "label=queue"],
                    "actions": {
                        "queue": {"name": "default", "require_branch_protection": False}
                    },
                },
            ],
        }

        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": [
                    "continuous-integration/fake-ci",
                ],
            },
            "required_linear_history": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }
        await self.setup_repo(yaml.dump(rules))
        await self.branch_protection_protect(self.main_branch_name, protection)

        p1 = await self.create_pr()
        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p1["base"]["sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p1["base"]["sha"],
                    "updated",
                    p1["number"],
                ),
            ],
        )

        # To force p1 to be rebased
        p2 = await self.create_pr()
        await self.merge_pull_as_admin(p2["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        p2 = await self.get_pull(p2["number"])

        ctxt = context.Context(self.repository_ctxt, p2)
        q = await merge_train.Train.from_context(ctxt)
        # base sha should have been updated
        await self._assert_cars_contents(
            q,
            p2["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p2["merge_commit_sha"],
                    "updated",
                    p1["number"],
                ),
            ],
        )

        # To force p1 to be rebased a second times
        p3 = await self.create_pr()
        await self.merge_pull_as_admin(p3["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        p3 = await self.get_pull(p3["number"])

        ctxt = context.Context(self.repository_ctxt, p3)
        q = await merge_train.Train.from_context(ctxt)
        # base sha should have been updated again
        await self._assert_cars_contents(
            q,
            p3["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p3["merge_commit_sha"],
                    "updated",
                    p1["number"],
                ),
            ],
        )

    async def test_queue_with_bot_account(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "draft_bot_account": "mergify-test4",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})
        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_pull_1 = await self.get_pull(pulls[1]["number"])
        tmp_pull_2 = await self.get_pull(pulls[0]["number"])
        assert tmp_pull_1["number"] not in [p1["number"], p2["number"]]
        assert tmp_pull_1["user"]["login"] == "mergify-test4"
        assert tmp_pull_2["number"] not in [p1["number"], p2["number"]]
        assert tmp_pull_2["user"]["login"] == "mergify-test4"

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
            ],
        )

        async def assert_queued():
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
            )
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )

        await self.run_engine()
        await assert_queued()
        assert tmp_pull_1["commits"] == 2
        assert tmp_pull_1["changed_files"] == 1
        assert tmp_pull_2["commits"] == 5
        assert tmp_pull_2["changed_files"] == 2

        await self.create_status(tmp_pull_2)
        await self.run_engine()
        await assert_queued()

        await self.create_status(tmp_pull_1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_queue_fast_forward(self):
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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {
                        "queue": {
                            "name": "default",
                            "priority": "high",
                            "method": "fast-forward",
                        }
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        pulls = await self.get_pulls()
        assert len(pulls) == 2

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "updated",
                    p1["number"],
                )
            ],
            [p2["number"]],
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

        await self.create_status(p1)
        await self.run_engine()

        p1 = await self.get_pull(p1["number"])
        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check["conclusion"] == "success"
        assert (
            check["output"]["title"] == "The pull request has been merged automatically"
        )
        assert (
            check["output"]["summary"]
            == f"The pull request has been merged automatically at *{p1['head']['sha']}*"
        )

        branch = typing.cast(
            github_types.GitHubBranch,
            await self.client_integration.item(
                f"{self.url_origin}/branches/{self.main_branch_name}"
            ),
        )
        assert p1["head"]["sha"] == branch["commit"]["sha"]

        # Continue with the second PR
        pulls = await self.get_pulls()
        assert len(pulls) == 1

        await self._assert_cars_contents(
            q,
            p1["head"]["sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [],
                    p1["head"]["sha"],
                    "updated",
                    p2["number"],
                )
            ],
        )

        head_sha = p2["head"]["sha"]
        p2 = await self.get_pull(p2["number"])
        assert p2["head"]["sha"] != head_sha  # ensure it have been rebased

        await self.run_engine()
        check = first(
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_status(p2)
        await self.run_engine()

        p2 = await self.get_pull(p2["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        check = first(
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check["conclusion"] == "success"
        assert (
            check["output"]["title"] == "The pull request has been merged automatically"
        )
        assert (
            check["output"]["summary"]
            == f"The pull request has been merged automatically at *{p2['head']['sha']}*"
        )

        branch = typing.cast(
            github_types.GitHubBranch,
            await self.client_integration.item(
                f"{self.url_origin}/branches/{self.main_branch_name}"
            ),
        )
        assert p2["head"]["sha"] == branch["commit"]["sha"]
        await self._assert_cars_contents(q, None, [])

    async def test_queue_with_ci_and_files(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        {
                            "or": [
                                "status-success=continuous-integration/fake-ci",
                                "files~=^.*\\.rst$",
                            ]
                        }
                    ],
                    "allow_inplace_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Queue",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()

        await self.add_label(p["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 2

        tmp_pull = await self.get_pull(p["number"] + 1)

        await self.create_status(tmp_pull, state="failure")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(q, None, [])
        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == "Queue: Embarked in merge train",
        )
        assert check is not None
        assert check["conclusion"] == "failure"

        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Queue (queue)",
        )
        assert check is not None
        assert check["conclusion"] == "cancelled"

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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})
        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_pull_1 = await self.get_pull(p["number"] + 1)
        tmp_pull_2 = await self.get_pull(p["number"] + 2)

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
            ],
        )

        async def assert_queued():
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
            )
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )

        await self.run_engine()
        await assert_queued()
        assert tmp_pull_1["commits"] == 2
        assert tmp_pull_1["changed_files"] == 1
        assert tmp_pull_2["commits"] == 5
        assert tmp_pull_2["changed_files"] == 2

        await self.create_status(tmp_pull_2)
        await self.run_engine()
        await assert_queued()

        await self.create_comment_as_admin(p1["number"], "@mergifyio refresh")
        await self.run_engine()
        await assert_queued()

        await self.create_status(tmp_pull_1, state="pending")
        await self.run_engine()
        await assert_queued()

        await self.create_status(tmp_pull_1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_queue_with_rebase_update_method(self):
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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {
                        "queue": {
                            "name": "default",
                            "priority": "high",
                            "update_method": "rebase",
                            "update_bot_account": "mergify-test4",
                        }
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        pulls = await self.get_pulls()
        assert len(pulls) == 2

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "updated",
                    p1["number"],
                ),
            ],
            [p2["number"]],
        )

        head_sha = p1["head"]["sha"]
        p1 = await self.get_pull(p1["number"])
        assert p1["head"]["sha"] != head_sha  # ensure it have been rebased
        commits = await self.get_commits(p1["number"])
        assert len(commits) == 1
        assert commits[0]["commit"]["committer"]["name"] == "mergify-test4"

        await self.run_engine()
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

        await self.wait_for("pull_request", {"action": "synchronize"})
        p1 = await self.get_pull(p1["number"])

        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [],
                    p1["merge_commit_sha"],
                    "updated",
                    p2["number"],
                ),
            ],
        )

        head_sha = p2["head"]["sha"]
        p2 = await self.get_pull(p2["number"])
        assert p2["head"]["sha"] != head_sha  # ensure it have been rebased
        commits = await self.get_commits(p2["number"])
        assert len(commits) == 2
        assert commits[0]["commit"]["committer"]["name"] == "mergify-test4"
        assert commits[1]["commit"]["committer"]["name"] == "mergify-test4"

        await self.create_status(p2)
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_queue_no_inplace(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 2

        tmp_pull = await self.get_pull(pulls[0]["number"])
        assert tmp_pull["number"] not in [p1["number"]]
        assert tmp_pull["changed_files"] == 1

        # No parent PR, but created instead updated
        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull["number"],
                ),
            ],
        )

        await self.create_status(tmp_pull)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])
        p1 = await self.get_pull(p1["number"])
        # ensure the MERGE QUEUE SUMMARY succeed
        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        assert check["conclusion"] == check_api.Conclusion.SUCCESS.value

    async def test_unqueue_rule_unmatch_with_batch_requeue(self) -> None:
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 1,
                    "allow_inplace_checks": True,
                    "batch_size": 3,
                    "batch_max_wait_time": "0 s",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)
        p3 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        await self.wait_for("pull_request", {"action": "opened"})
        await self.run_full_engine()
        tmp_pull_1 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 1)
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        assert p["merge_commit_sha"] is not None
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_1["number"],
                ),
            ],
        )

        await self.remove_label(p1["number"], "queue")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "opened"})
        tmp_pull_2 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 2)
        )
        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
            ],
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check is not None
        assert check["output"]["title"] == "The rule doesn't match anymore"

        check = first(
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check is not None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        check = first(
            await context.Context(self.repository_ctxt, p3).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check is not None
        assert (
            check["output"]["title"]
            == "The pull request is the 2nd in the queue to be merged"
        )

    async def test_unqueue_command_with_batch_requeue(self) -> None:
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 1,
                    "allow_inplace_checks": True,
                    "batch_size": 3,
                    "batch_max_wait_time": "0 s",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)
        p3 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        await self.wait_for("pull_request", {"action": "opened"})
        await self.run_engine()
        tmp_pull_1 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 1)
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        assert p["merge_commit_sha"] is not None
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_1["number"],
                ),
            ],
        )

        await self.create_comment_as_admin(p1["number"], "@mergifyio unqueue")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "opened"})
        tmp_pull_2 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 2)
        )
        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
            ],
        )

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
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
            await context.Context(self.repository_ctxt, p2).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check is not None
        assert check["conclusion"] is None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        check = first(
            await context.Context(self.repository_ctxt, p3).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert check is not None
        assert check["conclusion"] is None
        assert (
            check["output"]["title"]
            == "The pull request is the 2nd in the queue to be merged"
        )

    async def test_batch_queue(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 2,
                    "allow_inplace_checks": True,
                    "batch_size": 2,
                    "batch_max_wait_time": "0 s",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)
        p3 = await self.create_pr()
        p4 = await self.create_pr()
        p5 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.add_label(p4["number"], "queue")
        await self.add_label(p5["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 7

        tmp_pull_1 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 1)
        )
        tmp_pull_2 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 2)
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_1["number"],
                ),
                TrainCarMatcher(
                    [p3["number"], p4["number"]],
                    [p1["number"], p2["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
            ],
            [p5["number"]],
        )
        assert tmp_pull_1["changed_files"] == 2
        assert tmp_pull_2["changed_files"] == 4

        await self.create_status(tmp_pull_1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_pull_3 = await self.get_pull(
            github_types.GitHubPullRequestNumber(p["number"] + 3)
        )

        p2 = await self.get_pull(p2["number"])
        await self._assert_cars_contents(
            q,
            p2["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p3["number"], p4["number"]],
                    [p1["number"], p2["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pull_2["number"],
                ),
                TrainCarMatcher(
                    [p5["number"]],
                    [p3["number"], p4["number"]],
                    p2["merge_commit_sha"],
                    "created",
                    tmp_pull_3["number"],
                ),
            ],
        )
        assert tmp_pull_2["changed_files"] == 4
        assert tmp_pull_3["changed_files"] == 3

        await self.create_status(tmp_pull_2)
        await self.run_engine()
        await self.create_status(tmp_pull_3)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_batch_split_with_no_speculative_checks(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "batch_max_wait_time": "0 s",
                    "allow_inplace_checks": False,
                    "speculative_checks": 1,
                    "batch_size": 3,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr()
        p4 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.add_label(p4["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [p4["number"]],
        )

        await self.create_status(tmp_pulls[0], state="failure")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 6
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        # The train car has been splitted, the second car is in pending
        # state as speculative_checks=1
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[1]["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "pending",
                    None,
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [p4["number"]],
        )

        # Merge p1, p2 should be started to be checked, second car must go to
        # created creation_state
        await self.create_status(tmp_pulls[1])
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]

        pulls = await self.get_pulls()
        assert len(pulls) == 5
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[1]["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [p4["number"]],
        )

        # It's fault of p2!
        await self.create_status(tmp_pulls[1], state="failure")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        # Thing move on and restart from p3 but based on p1 merge commit
        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p3["number"], p4["number"]],
                    [],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [],
        )

    async def test_batch_split_queue(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 2,
                    "batch_size": 3,
                    "batch_max_wait_time": "0 s",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)
        p3 = await self.create_pr()
        p4 = await self.create_pr()
        p5 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.add_label(p4["number"], "queue")
        await self.add_label(p5["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 7

        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p5["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"], p3["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
                TrainCarMatcher(
                    [p4["number"], p5["number"]],
                    [p1["number"], p2["number"], p3["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[1]["number"],
                ),
            ],
        )

        await self.create_status(tmp_pulls[0], state="failure")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 8
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p5["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        # The train car has been splitted
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[1]["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[2]["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [p4["number"], p5["number"]],
        )

        # Merge p1 and p2, p3 should be dropped and p4 et p5 checked
        await self.create_status(tmp_pulls[1])
        await self.create_status(tmp_pulls[2])
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p3["number"],
                    p4["number"],
                    p5["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        p2 = await self.get_pull(p2["number"])
        await self._assert_cars_contents(
            q,
            p2["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p4["number"], p5["number"]],
                    [],
                    p2["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
        )

        await self.create_status(tmp_pulls[0])
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 1

        await self._assert_cars_contents(q, None, [])

    async def test_first_batch_split_queue(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "batch_max_wait_time": "0 s",
                    "speculative_checks": 2,
                    "batch_size": 3,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        # To force others to create draft PR
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p2["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
        )

        await self.create_status(tmp_pulls[0], state="failure")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4
        tmp_pulls = sorted(
            [
                tmp
                for tmp in pulls
                if tmp["number"]
                not in (
                    p1["number"],
                    p2["number"],
                    p["number"],
                )
            ],
            key=operator.itemgetter("number"),
        )

        # The train car has been splitted
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[1]["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_pulls[0]["number"],
                ),
            ],
            [],
        )

        # Merge p1, p2 should be marked as failure
        await self.create_status(tmp_pulls[1])
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 1

        await self._assert_cars_contents(q, None, [])

    async def test_queue_just_rebase(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": ["label=queue"],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        p_other = await self.create_pr()
        await self.merge_pull(p_other["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        await self.add_label(p["number"], "queue")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(q, None, [])

        pulls = await self.get_pulls()
        assert len(pulls) == 0

    async def test_queue_already_ready(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": ["label=queue"],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()

        await self.add_label(p["number"], "queue")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(q, None, [])

        pulls = await self.get_pulls()
        assert len(pulls) == 0

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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})
        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = await self.get_pull(pulls[1]["number"])
        tmp_mq_p2 = await self.get_pull(pulls[0]["number"])
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        assert tmp_mq_p1["commits"] == 2
        assert tmp_mq_p1["changed_files"] == 1
        assert tmp_mq_p2["commits"] == 4
        assert tmp_mq_p2["changed_files"] == 2
        await self.create_status(tmp_mq_p2)
        await self.run_engine()

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

        await self.create_status(tmp_mq_p1, state="pending")
        await self.create_status(tmp_mq_p2, state="pending")
        await self.run_engine()
        await assert_queued(p1)
        await assert_queued(p2)

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        await self.create_status(tmp_mq_p1)
        await self.create_status(tmp_mq_p2)
        await self.run_engine()
        await assert_queued(p1)
        await assert_queued(p2)

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        await self.add_label(p1["number"], "foobar")
        await self.add_label(p2["number"], "foobar")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

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
                        f"base={self.main_branch_name}",
                        "label=queue",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.create_status(p1)
        await self.add_label(p1["number"], "queue")
        await self.create_status(p2)
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})
        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = await self.get_pull(pulls[1]["number"])
        tmp_mq_p2 = await self.get_pull(pulls[0]["number"])
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        assert tmp_mq_p1["commits"] == 2
        assert tmp_mq_p1["changed_files"] == 1
        assert tmp_mq_p2["commits"] == 5
        assert tmp_mq_p2["changed_files"] == 2
        await self.create_status(tmp_mq_p2)

        await self.run_engine()
        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )
        await self.create_status(tmp_mq_p1, state="pending")
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
        await self.create_status(tmp_mq_p1, state="success")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_merge_queue_refresh(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": True,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1["number"], p2["number"]]

        mq_pr_number = q._cars[1].queue_pull_request_number

        await self.create_comment_as_admin(mq_pr_number, "@mergifyio update")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(mq_pr_number)
        assert (
            "Command not allowed on merge queue pull request." == comments[-1]["body"]
        )

        await self.create_comment_as_admin(mq_pr_number, "@mergifyio refresh")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(mq_pr_number)
        assert (
            """> refresh

#### ✅ Pull request refreshed



<!--
DO NOT EDIT
-*- Mergify Payload -*-
{"command": "refresh", "conclusion": "success"}
-*- Mergify Payload End -*-
-->
"""
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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
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
        # 2 queue PR with its tmp PR + 1 one not queued PR
        assert len(pulls) == 5

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Merge p1
        await self.create_status(tmp_mq_p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()
        pulls = await self.get_pulls()
        assert len(pulls) == 3
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]

        # ensure base is p, it's tested with p1, but current_base_sha have changed since
        # we create the tmp pull request
        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
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
            p1["merge_commit_sha"],
            [
                # Ensure p2 car is still the same
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                # Ensure base is p1 and only p2 is tested with p3
                TrainCarMatcher(
                    [p3["number"]],
                    [p2["number"]],
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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Queue two pulls
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
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
        assert len(pulls) == 3

        # Nothing change
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )
        p2 = await self.get_pull(p2["number"])
        assert not p2["merged"]

        # p1 is ready, check both are merged in a row
        await self.create_status(tmp_mq_p1)
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0
        p1 = await self.get_pull(p1["number"])
        assert p1["merged"]
        p2 = await self.get_pull(p2["number"])
        assert p2["merged"]

        await self._assert_cars_contents(q, None, [])

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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # tmp merge-queue pull p2 fail
        await self.create_status(tmp_mq_p2, state="failure")
        await self.run_engine()

        await self.create_status(tmp_mq_p1, state="failure")
        await self.run_engine()

        # TODO(sileht): Add some assertion on check-runs content

        # tmp merge-queue pull p2 have been closed and p2 updated/rebased
        pulls = await self.get_pulls()
        assert len(pulls) == 3
        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p2 != tmp_mq_p2_bis
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2_bis["number"],
                ),
            ],
        )

        # Merge p2
        await self.create_status(tmp_mq_p2_bis)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        # Only p1 is still there and the queue is empty
        pulls = await self.get_pulls()
        assert len(pulls) == 1
        assert pulls[0]["number"] == p1["number"]
        await self._assert_cars_contents(q, None, [])

    async def test_batch_cant_create_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "batch_size": 2,
                    "speculative_checks": 1,
                    "allow_inplace_checks": True,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p1 = await self.create_pr(files={"conflicts": "well"})
        p2 = await self.create_pr(files={"conflicts": "boom"})
        p3 = await self.create_pr()

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4, [p["number"] for p in pulls]

        tmp_mq = pulls[0]
        assert tmp_mq["number"] not in [p1["number"], p2["number"], p3["number"]]

        # Check only p1 and p3 are in the train
        ctxt_p1 = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt_p1)
        await self._assert_cars_contents(
            q,
            p1["base"]["sha"],
            [
                TrainCarMatcher(
                    [p1["number"], p3["number"]],
                    [],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq["number"],
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
        assert check["output"]["summary"] == (
            "The merge-queue pull request can't be created\n"
            f"Details: `The pull request conflict with at least one of pull request ahead in queue: #{p1['number']}`"
        )

        # Merge the train
        await self.create_status(tmp_mq)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        # Only p2 is remaining and not in train
        pulls = await self.get_pulls()
        assert len(pulls) == 1
        assert pulls[0]["number"] == p2["number"]

        await self._assert_cars_contents(q, None, [])

    async def test_queue_cant_create_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": True,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p1 = await self.create_pr(files={"conflicts": "well"})
        p2 = await self.create_pr(files={"conflicts": "boom"})
        p3 = await self.create_pr()

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.add_label(p3["number"], "queue")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4, [p["number"] for p in pulls]

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3["number"] not in [p1["number"], p2["number"], p3["number"]]

        # Check only p1 and p3 are in the train
        ctxt_p1 = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt_p1)
        await self._assert_cars_contents(
            q,
            p1["base"]["sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p1["base"]["sha"],
                    "updated",
                    p1["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"]],
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
        assert check["output"]["summary"] == (
            "The merge-queue pull request can't be created\n"
            f"Details: `The pull request conflict with at least one of pull request ahead in queue: #{p1['number']}`"
        )

        # Merge the train
        await self.create_status(p1)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.create_status(tmp_mq_p3)
        await self.run_engine()
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        # Only p2 is remaining and not in train
        pulls = await self.get_pulls()
        assert len(pulls) == 1
        assert pulls[0]["number"] == p2["number"]

        await self._assert_cars_contents(q, None, [])

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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr()

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
            p1["base"]["sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p1["base"]["sha"],
                    "updated",
                    p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
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
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p1["base"]["sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
                    p1["base"]["sha"],
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
        assert len(pulls) == 3

        tmp_mq_p3_bis = await self.get_pull(tmp_mq_p3["number"] + 1)
        # p3 get a new draft PR
        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p3["number"]],
                    [],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p3_bis["number"],
                ),
            ],
        )

        # refresh to add it back in queue
        check = typing.cast(
            github_types.GitHubCheckRun,
            await self.client_integration.items(
                f"{self.url_origin}/commits/{p2['head']['sha']}/check-runs",
                resource_name="check runs",
                page_limit=5,
                api_version="antiope",
                list_items="check_runs",
                params={"name": constants.MERGE_QUEUE_SUMMARY_NAME},
            ).__anext__(),
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
        assert len(pulls) == 4

        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p2_bis["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p1["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p3["number"]],
                    [],
                    p1["merge_commit_sha"],
                    "created",
                    tmp_mq_p3_bis["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p3["number"]],
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
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Queue PRs
        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")

        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"]]

        ctxt_p_merged = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt_p_merged)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Merge a not queued PR manually
        p_merged_in_meantime = await self.create_pr()
        await self.merge_pull(p_merged_in_meantime["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        p_merged_in_meantime = await self.get_pull(p_merged_in_meantime["number"])

        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1_bis = await self.get_pull(pulls[1]["number"])
        tmp_mq_p2_bis = await self.get_pull(pulls[0]["number"])
        assert tmp_mq_p1_bis["number"] not in [
            p1["number"],
            p2["number"],
            tmp_mq_p1["number"],
            tmp_mq_p2["number"],
        ]
        assert tmp_mq_p2_bis["number"] not in [
            p1["number"],
            p2["number"],
            tmp_mq_p1["number"],
            tmp_mq_p2["number"],
        ]

        await self._assert_cars_contents(
            q,
            p_merged_in_meantime["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p_merged_in_meantime["merge_commit_sha"],
                    "created",
                    tmp_mq_p1_bis["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p_merged_in_meantime["merge_commit_sha"],
                    "created",
                    tmp_mq_p2_bis["number"],
                ),
            ],
        )

        # Ensurep1 and p2 got recreate with more commits
        assert tmp_mq_p1_bis["commits"] == 2
        assert tmp_mq_p1_bis["changed_files"] == 1
        assert tmp_mq_p2_bis["commits"] == 4
        assert tmp_mq_p2_bis["changed_files"] == 2

        # Merge the train
        await self.create_status(tmp_mq_p1_bis)
        await self.create_status(tmp_mq_p2_bis)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_queue_pr_priority_no_interrupt(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/slow-ci",
                    ],
                    "allow_inplace_checks": False,
                    "speculative_checks": 5,
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=high",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=low",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "low"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr()

        # To force others to be rebased
        p_merged = await self.create_pr()
        await self.merge_pull(p_merged["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p_merged = await self.get_pull(p_merged["number"])

        # Put first PR in queue
        await self.add_label(p1["number"], "low")
        await self.add_label(p2["number"], "low")
        await self.run_engine()

        ctxt_p_merged = context.Context(self.repository_ctxt, p_merged)
        q = await merge_train.Train.from_context(ctxt_p_merged)

        # my 3 PRs + 2 merge-queue PR
        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"], p3["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p_merged["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Change the configuration and introduce disallow_checks_interruption_from_queues
        updated_rules = copy.deepcopy(rules)
        updated_rules["queue_rules"][0]["disallow_checks_interruption_from_queues"] = [
            "default"
        ]
        p_new_config = await self.create_pr(
            files={".mergify.yml": yaml.dump(updated_rules)}
        )
        await self.merge_pull(p_new_config["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p_new_config = await self.get_pull(p_new_config["number"])

        ctxt_p_new_config = context.Context(self.repository_ctxt, p_new_config)
        q = await merge_train.Train.from_context(ctxt_p_new_config)

        # my 3 PRs + 2 merge-queue PR
        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"], p3["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p_new_config["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p_new_config["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p_new_config["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Put second PR at the begining of the queue via pr priority Checks
        # must not be interrupted due to
        # disallow_checks_interruption_from_queues config
        await self.add_label(p3["number"], "high")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 6

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p_new_config["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p_new_config["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p_new_config["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
                TrainCarMatcher(
                    [p3["number"]],
                    [p1["number"], p2["number"]],
                    p_new_config["merge_commit_sha"],
                    "created",
                    tmp_mq_p3["number"],
                ),
            ],
        )

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
                        f"base={self.main_branch_name}",
                        "label=queue-urgent",
                    ],
                    "actions": {"queue": {"name": "urgent"}},
                },
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr()

        # To force others to be rebased
        p_merged = await self.create_pr()
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

        # my 3 PRs + 2 merge-queue PR
        pulls = await self.get_pulls()
        assert len(pulls) == 5

        tmp_mq_p1 = pulls[1]
        tmp_mq_p2 = pulls[0]
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"], p3["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p_merged["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        # Put third PR at the begining of the queue via queue priority
        await self.add_label(p3["number"], "queue-urgent")
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p3 = pulls[0]
        assert tmp_mq_p3["number"] not in [p1["number"], p2["number"], p3["number"]]

        # p3 is now the only car in train, as its queue is not the same as p1 and p2
        await self._assert_cars_contents(
            q,
            p_merged["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p3["number"]],
                    [],
                    p_merged["merge_commit_sha"],
                    "created",
                    tmp_mq_p3["number"],
                ),
            ],
            [p1["number"], p2["number"]],
        )

        # Queue API with token
        r = await self.app.get(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/queues",
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200
        assert r.json() == {
            "queues": [
                {
                    "branch": {"name": self.main_branch_name},
                    "pull_requests": [
                        {
                            "number": p3["number"],
                            "position": 0,
                            "queued_at": mock.ANY,
                            "priority": 2000,
                            "queue_rule": {
                                "config": {
                                    "allow_inplace_checks": True,
                                    "disallow_checks_interruption_from_queues": [],
                                    "batch_max_wait_time": 30.0,
                                    "batch_size": 1,
                                    "checks_timeout": None,
                                    "draft_bot_account": None,
                                    "priority": 1,
                                    "speculative_checks": 5,
                                },
                                "name": "urgent",
                            },
                            "speculative_check_pull_request": {
                                "in_place": False,
                                "number": tmp_mq_p3["number"],
                                "started_at": mock.ANY,
                                "ended_at": mock.ANY,
                                "state": "pending",
                                "checks": [],
                                "evaluated_conditions": "- [ ] `status-success=continuous-integration/fast-ci`\n",
                            },
                        },
                        {
                            "number": p1["number"],
                            "position": 1,
                            "priority": 2000,
                            "queue_rule": {
                                "config": {
                                    "allow_inplace_checks": True,
                                    "disallow_checks_interruption_from_queues": [],
                                    "batch_max_wait_time": 30.0,
                                    "batch_size": 1,
                                    "checks_timeout": None,
                                    "draft_bot_account": None,
                                    "priority": 0,
                                    "speculative_checks": 5,
                                },
                                "name": "default",
                            },
                            "queued_at": mock.ANY,
                            "speculative_check_pull_request": None,
                        },
                        {
                            "number": p2["number"],
                            "position": 2,
                            "priority": 2000,
                            "queue_rule": {
                                "config": {
                                    "allow_inplace_checks": True,
                                    "disallow_checks_interruption_from_queues": [],
                                    "batch_size": 1,
                                    "batch_max_wait_time": 30.0,
                                    "checks_timeout": None,
                                    "draft_bot_account": None,
                                    "priority": 0,
                                    "speculative_checks": 5,
                                },
                                "name": "default",
                            },
                            "queued_at": mock.ANY,
                            "speculative_check_pull_request": None,
                        },
                    ],
                }
            ],
        }

        # Merge p3
        await self.create_status(tmp_mq_p3, context="continuous-integration/fast-ci")
        await self.run_engine()
        p3 = await self.get_pull(p3["number"])
        assert p3["merged"]

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        # ensure p1 and p2 are back in queue
        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1_bis = pulls[1]
        tmp_mq_p2_bis = pulls[0]
        assert tmp_mq_p1_bis["number"] not in [p1["number"], p2["number"], p3["number"]]
        assert tmp_mq_p2_bis["number"] not in [p1["number"], p2["number"], p3["number"]]

        await self._assert_cars_contents(
            q,
            p3["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p3["merge_commit_sha"],
                    "created",
                    tmp_mq_p1_bis["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p3["merge_commit_sha"],
                    "created",
                    tmp_mq_p2_bis["number"],
                ),
            ],
        )

    async def test_queue_no_tmp_pull_request(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "speculative_checks": 5,
                    "allow_inplace_checks": True,
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge train",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()
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
    # @pytest.mark.skipif(
    #    config.GITHUB_URL != "https://github.com",
    #    reason="We use a PAT token instead of an OAUTH_TOKEN",
    # )
    # MRGFY-472 should fix that
    @pytest.mark.skip(
        reason="This test is not reliable, GitHub doeesn't always allow to create the tmp pr"
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
                    "allow_inplace_checks": True,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
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

        p1 = await self.create_pr()
        p2 = await self.create_pr(two_commits=True)

        # To force others to be rebased
        p = await self.create_pr(
            files={
                ".github/workflows/ci.yml": TEMPLATE_GITHUB_ACTION % "echo Changed CI"
            }
        )

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        # Merge base branch into p2
        await self.client_integration.put(
            f"{self.url_origin}/pulls/{p2['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": p2["head"]["sha"]},
        )

        await self.add_label(p1["number"], "queue")
        await self.add_label(p2["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "opened"})
        await self.wait_for("pull_request", {"action": "opened"})

        pulls = await self.get_pulls()
        assert len(pulls) == 4

        tmp_mq_p1 = await self.get_pull(pulls[1]["number"])
        tmp_mq_p2 = await self.get_pull(pulls[0]["number"])
        assert tmp_mq_p1["number"] not in [p1["number"], p2["number"]]
        assert tmp_mq_p2["number"] not in [p1["number"], p2["number"]]

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)

        await self._assert_cars_contents(
            p["merge_commit_sha"],
            q,
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p1["number"],
                ),
                TrainCarMatcher(
                    [p2["number"]],
                    [p1["number"]],
                    p["merge_commit_sha"],
                    "created",
                    tmp_mq_p2["number"],
                ),
            ],
        )

        assert tmp_mq_p1["commits"] == 7
        assert tmp_mq_p1["changed_files"] == 1
        assert tmp_mq_p2["commits"] == 7
        assert tmp_mq_p2["changed_files"] == 5
        await self.create_status(tmp_mq_p2)

        await self.run_engine()

        check = first(
            await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge priority high (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_status(tmp_mq_p1)
        await self.run_engine()

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_more_ci_in_pull_request_rules_succeed(self):
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
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "status-success=very-long-ci",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
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
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "updated",
                    p1["number"],
                ),
            ],
        )

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
        await self.create_status(p1)
        await self.run_engine()

        await assert_queued()
        await self.create_status(p1, context="very-long-ci")
        await self.run_engine()

        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_more_ci_in_pull_request_rules_failure(self):
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
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "status-success=very-long-ci",
                        "label=queue",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
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
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "updated",
                    p1["number"],
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

        await self._assert_cars_contents(q, None, [])

    async def test_queue_ci_timeout_inplace(self) -> None:
        config = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "checks_timeout": "10 m",
                }
            ],
            "pull_request_rules": [
                {
                    "name": "queue",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        with freeze_time("2021-05-30T10:00:00", tick=True):
            await self.setup_repo(yaml.dump(config))

            p1 = await self.create_pr()

            # To force others to be rebased
            p = await self.create_pr()
            await self.merge_pull(p["number"])
            await self.wait_for("pull_request", {"action": "closed"})
            await self.run_full_engine()

            await self.create_status(p1)
            await self.run_full_engine()

            await self.wait_for("pull_request", {"action": "synchronize"})
            await self.run_full_engine()

            # p1 has been rebased
            p1 = await self.get_pull(p1["number"])

            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )
            pulls_to_refresh: typing.List[
                typing.Tuple[str, float]
            ] = await self.redis_links.cache.zrangebyscore(
                "delayed-refresh", "-inf", "+inf", withscores=True
            )
            assert len(pulls_to_refresh) == 1

        with freeze_time("2021-05-30T10:12:00", tick=True):

            await self.run_full_engine()
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request has been removed from the queue"
            )
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Queue: Embarked in merge train",
            )
            assert check is not None
            assert "checks have timed out" in check["output"]["summary"]

    async def test_queue_ci_timeout_draft_pr(self) -> None:
        config = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "checks_timeout": "10 m",
                    "allow_inplace_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "queue",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }
        with freeze_time("2021-05-30T10:00:00", tick=True):
            await self.setup_repo(yaml.dump(config))

            p1 = await self.create_pr()

            # To force others to be rebased
            p = await self.create_pr()
            await self.merge_pull(p["number"])
            await self.wait_for("pull_request", {"action": "closed"})
            await self.run_full_engine()

            await self.create_status(p1)
            await self.run_full_engine()

            await self.wait_for("pull_request", {"action": "opened"})
            await self.run_full_engine()

            # p1 has been rebased
            p1 = await self.get_pull(p1["number"])

            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )
            pulls_to_refresh: typing.List[
                typing.Tuple[str, float]
            ] = await self.redis_links.cache.zrangebyscore(
                "delayed-refresh", "-inf", "+inf", withscores=True
            )
            assert len(pulls_to_refresh) == 1

        with freeze_time("2021-05-30T10:12:00", tick=True):

            await self.run_full_engine()
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request has been removed from the queue"
            )
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Queue: Embarked in merge train",
            )
            assert check is not None
            assert "checks have timed out" in check["output"]["summary"]

    async def test_queue_ci_timeout_outside_schedule_without_unqueuing(self) -> None:
        config = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        {
                            "or": [
                                "check-success=continuous-integration/fake-ci",
                                "check-success=continuous-integration/other-ci",
                            ]
                        },
                        "schedule: MON-FRI 08:00-17:00",
                    ],
                    "checks_timeout": "10 m",
                    "allow_inplace_checks": False,
                }
            ],
            "pull_request_rules": [
                {
                    "name": "queue",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
            ],
        }

        with freeze_time("2021-05-30T20:00:00", tick=True):
            await self.setup_repo(yaml.dump(config))

            p1 = await self.create_pr()

            # To force others to be rebased
            p = await self.create_pr()
            await self.merge_pull(p["number"])
            await self.wait_for("pull_request", {"action": "closed"})
            await self.run_full_engine()

            await self.create_status(p1)
            await self.run_full_engine()

            await self.wait_for("pull_request", {"action": "opened"})
            await self.run_full_engine()

            # p1 has been rebased
            p1 = await self.get_pull(p1["number"])

            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )
            pulls_to_refresh: typing.List[
                typing.Tuple[str, float]
            ] = await self.redis_links.cache.zrangebyscore(
                "delayed-refresh", "-inf", "+inf", withscores=True
            )
            assert len(pulls_to_refresh) == 1
            tmp_pull = await self.get_pull(
                github_types.GitHubPullRequestNumber(p["number"] + 1)
            )
            await self.create_status(tmp_pull)

        with freeze_time("2021-05-30T20:12:00", tick=True):

            await self.run_full_engine()
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Rule: queue (queue)",
            )
            assert check is not None
            assert (
                check["output"]["title"]
                == "The pull request is the 1st in the queue to be merged"
            )
            check = first(
                await context.Context(self.repository_ctxt, p1).pull_engine_check_runs,
                key=lambda c: c["name"] == "Queue: Embarked in merge train",
            )
            assert check is not None
            assert "checks have timed out" not in check["output"]["summary"]

    async def test_queue_without_branch_protection_for_queueing(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=queue",
                    ],
                    "actions": {
                        "queue": {
                            "method": "squash",
                            "name": "default",
                            "require_branch_protection": False,
                        }
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": [
                    "continuous-integration/fake-ci",
                ],
            },
            "required_linear_history": True,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p1 = await self.create_pr()

        # To force others to be rebased
        p = await self.create_pr()
        await self.merge_pull_as_admin(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p = await self.get_pull(p["number"])

        await self.add_label(p1["number"], "queue")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p1["number"]],
                    [],
                    p["merge_commit_sha"],
                    "updated",
                    p1["number"],
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
        await self.wait_for("pull_request", {"action": "closed"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])

    async def test_queue_checks_and_branch(self):
        rules = f"""
queue_rules:
  - name: default
    conditions:
      - "check-success=Summary"
      - "check-success=ci/status"
      - "check-success=ci/service-test"
      - "check-success=ci/pipelines"
      - "#approved-reviews-by>=1"
      - "-label=flag:wait"

pull_request_rules:
  - name: merge
    conditions:
      - "-draft"
      - "-closed"
      - "-merged"
      - "-conflict"
      - "base={self.main_branch_name}"
      - "label=flag:merge"
    actions:
      queue:
        name: default
        priority: medium
        update_method: rebase
        require_branch_protection: false
"""
        await self.setup_repo(rules)

        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": [
                    "ci/status",
                    "ci/service-test",
                    "ci/pipelines",
                ],
            },
            "required_linear_history": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p = await self.create_pr()

        # To force others to be rebased
        p_other = await self.create_pr()
        await self.merge_pull_as_admin(p_other["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        p_other = await self.get_pull(p_other["number"])

        await self.create_review(p["number"])
        await self.add_label(p["number"], "flag:merge")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p_other)
        q = await merge_train.Train.from_context(ctxt)
        await self._assert_cars_contents(
            q,
            p_other["merge_commit_sha"],
            [
                TrainCarMatcher(
                    [p["number"]],
                    [],
                    p_other["merge_commit_sha"],
                    "updated",
                    p["number"],
                ),
            ],
        )

        head_sha = p["head"]["sha"]
        p = await self.get_pull(p["number"])
        assert p["head"]["sha"] != head_sha  # ensure it have been rebased

        check = first(
            await context.Context(self.repository_ctxt, p).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: merge (queue)",
        )
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        await self.create_status(p, "ci/status", state="pending")
        await self.run_engine()

        await self.create_status(p, "ci/status")
        await self.create_status(p, "ci/service-test")
        await self.run_engine()

        await self.create_status(p, "ci/pipelines")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        pulls = await self.get_pulls()
        assert len(pulls) == 0

        await self._assert_cars_contents(q, None, [])


class TestTrainApiCalls(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_create_pull_basic(self):
        config = {
            "queue_rules": [
                {
                    "name": "foo",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "queue",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "foo"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(config))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        base_sha = await q.get_base_sha()

        queue_config = rules.QueueConfig(
            priority=0,
            speculative_checks=5,
            batch_size=1,
            batch_max_wait_time=datetime.timedelta(seconds=0),
            allow_inplace_checks=True,
            disallow_checks_interruption_from_queues=[],
            checks_timeout=None,
            draft_bot_account=None,
        )
        config = queue.PullQueueConfig(
            name="foo",
            strict_method="merge",
            update_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
        )

        car = merge_train.TrainCar(
            q,
            [merge_train.EmbarkedPull(q, p2["number"], config, date.utcnow())],
            [merge_train.EmbarkedPull(q, p2["number"], config, date.utcnow())],
            [p1["number"]],
            base_sha,
        )
        q._cars.append(car)

        queue_rule = rules.QueueRule(
            name="foo",
            conditions=conditions.QueueRuleConditions([]),
            config=queue_config,
        )
        await car.start_checking_with_draft(queue_rule)
        assert car.queue_pull_request_number is not None
        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = [p for p in pulls if p["number"] == car.queue_pull_request_number][0]
        assert tmp_pull["draft"]

        pull_url_prefix = f"/{self.installation_ctxt.owner_login}/{self.repository_ctxt.repo['name']}/pull"
        expected_table = f"| 1 | test_create_pull_basic: pull request n2 from integration ([#{p2['number']}]({pull_url_prefix}/{p2['number']})) | foo/0 | [#{tmp_pull['number']}]({pull_url_prefix}/{tmp_pull['number']}) | <fake_pretty_datetime()>|"
        assert expected_table in await car.generate_merge_queue_summary()

        await car.delete_pull(reason="testing deleted reason")

        ctxt = context.Context(self.repository_ctxt, tmp_pull)
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert summary["conclusion"] == "cancelled"
        assert "testing deleted reason" in summary["output"]["summary"]

        pulls = await self.get_pulls()
        assert len(pulls) == 2

    async def test_create_pull_after_failure(self):
        config = {
            "queue_rules": [
                {
                    "name": "foo",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "queue",
                    "conditions": [
                        "check-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"queue": {"name": "foo"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(config))

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        ctxt = context.Context(self.repository_ctxt, p1)
        q = await merge_train.Train.from_context(ctxt)
        base_sha = await q.get_base_sha()

        queue_config = rules.QueueConfig(
            priority=0,
            speculative_checks=5,
            batch_size=1,
            batch_max_wait_time=datetime.timedelta(seconds=0),
            allow_inplace_checks=True,
            disallow_checks_interruption_from_queues=[],
            checks_timeout=None,
            draft_bot_account=None,
        )
        config = queue.PullQueueConfig(
            name="foo",
            strict_method="merge",
            update_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
        )

        car = merge_train.TrainCar(
            q,
            [merge_train.EmbarkedPull(q, p2["number"], config, date.utcnow())],
            [merge_train.EmbarkedPull(q, p2["number"], config, date.utcnow())],
            [p1["number"]],
            base_sha,
        )
        q._cars.append(car)

        queue_rule = rules.QueueRule(
            name="foo",
            conditions=conditions.QueueRuleConditions([]),
            config=queue_config,
        )
        await car.start_checking_with_draft(queue_rule)
        assert car.queue_pull_request_number is not None
        pulls = await self.get_pulls()
        assert len(pulls) == 3

        tmp_pull = [p for p in pulls if p["number"] == car.queue_pull_request_number][0]
        assert tmp_pull["draft"]

        # Ensure pull request is closed
        branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{car.train.ref}/{car.head_branch}"
        )
        await car._prepare_empty_draft_pr_branch(branch_name, None)
        await self.wait_for("pull_request", {"action": "closed"})
        pulls = await self.get_pulls()
        assert len(pulls) == 2

        # Recreating it should works
        await car.start_checking_with_draft(queue_rule)
        assert car.queue_pull_request_number is not None
        pulls = await self.get_pulls()
        assert len(pulls) == 3
        tmp_pull = [p for p in pulls if p["number"] == car.queue_pull_request_number][0]
        assert tmp_pull["draft"]

    async def test_create_pull_conflicts(self):
        await self.setup_repo(yaml.dump({}), files={"conflicts": "foobar"})

        p = await self.create_pr(files={"conflicts": "well"})
        p1 = await self.create_pr()
        p2 = await self.create_pr()
        p3 = await self.create_pr(files={"conflicts": "boom"})

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.repository_ctxt, p)
        q = await merge_train.Train.from_context(ctxt)
        base_sha = await q.get_base_sha()

        queue_config = rules.QueueConfig(
            priority=0,
            speculative_checks=5,
            batch_size=1,
            batch_max_wait_time=datetime.timedelta(seconds=0),
            allow_inplace_checks=True,
            disallow_checks_interruption_from_queues=[],
            checks_timeout=None,
            draft_bot_account=None,
        )
        config = queue.PullQueueConfig(
            name="foo",
            strict_method="merge",
            update_method="merge",
            priority=0,
            effective_priority=0,
            bot_account=None,
            update_bot_account=None,
        )

        car = merge_train.TrainCar(
            q,
            [merge_train.EmbarkedPull(q, p3["number"], config, date.utcnow())],
            [merge_train.EmbarkedPull(q, p3["number"], config, date.utcnow())],
            [p1["number"], p2["number"]],
            base_sha,
        )
        with pytest.raises(merge_train.TrainCarPullRequestCreationFailure) as exc_info:
            await car.start_checking_with_draft(
                rules.QueueRule(
                    name="foo",
                    conditions=conditions.QueueRuleConditions([]),
                    config=queue_config,
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
        assert check["output"]["summary"] == (
            "The merge-queue pull request can't be created\n"
            "Details: `The pull request conflict with at least one of pull request ahead in queue: "
            f"#{p1['number']}, #{p2['number']}`"
        )
