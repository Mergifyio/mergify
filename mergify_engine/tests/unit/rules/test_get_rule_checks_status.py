# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
import dataclasses
import typing
from unittest import mock

from freezegun import freeze_time
import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import date
from mergify_engine import rules
from mergify_engine.rules import checks_status
from mergify_engine.rules import conditions
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers


FAKE_REPO = mock.Mock(repo={"owner": {"login": "org"}})


@dataclasses.dataclass
class FakeQueuePullRequest:
    attrs: typing.Dict[str, context.ContextAttributeType]

    async def __getattr__(self, name: str) -> context.ContextAttributeType:
        fancy_name = name.replace("_", "-")
        try:
            return self.attrs[fancy_name]
        except KeyError:
            raise context.PullRequestAttributeError(name=fancy_name)

    def sync_checks(self) -> None:
        self.attrs["check-success-or-neutral"] = (
            self.attrs.get("check-success", [])  # type: ignore
            + self.attrs.get("check-neutral", [])  # type: ignore
            + self.attrs.get("check-pending", [])  # type: ignore
        )
        self.attrs["check-success-or-neutral-or-pending"] = (
            self.attrs.get("check-success", [])  # type: ignore
            + self.attrs.get("check-neutral", [])  # type: ignore
            + self.attrs.get("check-pending", [])  # type: ignore
        )
        self.attrs["check"] = (
            self.attrs.get("check-success", [])  # type: ignore
            + self.attrs.get("check-neutral", [])  # type: ignore
            + self.attrs.get("check-pending", [])  # type: ignore
            + self.attrs.get("check-failure", [])  # type: ignore
            + self.attrs.get("check-skipped", [])  # type: ignore
        )

        self.attrs["status-success"] = self.attrs.get("check-success", [])  # type: ignore
        self.attrs["status-neutral"] = self.attrs.get("check-neutral", [])  # type: ignore
        self.attrs["status-failure"] = self.attrs.get("check-failure", [])  # type: ignore


async def test_rules_conditions_update() -> None:
    pulls = [
        FakeQueuePullRequest(
            {
                "number": 1,
                "current-year": date.Year(2018),
                "author": "me",
                "base": "main",
                "head": "feature-1",
                "label": ["foo", "bar"],
                "check-success": ["tests"],
                "check-pending": [],  # type: ignore
                "check-failure": ["jenkins/fake-tests"],
                "check-skipped": [],  # type: ignore
                "check-stale": [],  # type: ignore
            }
        ),
    ]
    pulls[0].sync_checks()
    schema = voluptuous.Schema(
        voluptuous.All(
            [voluptuous.Coerce(rules.RuleConditionSchema)],
            voluptuous.Coerce(conditions.QueueRuleConditions),
        )
    )

    c = schema(
        [
            "label=foo",
            "check-success=tests",
            "check-success=jenkins/fake-tests",
        ]
    )

    await c(pulls)

    assert (
        c.get_summary()
        == """- `label=foo`
  - [X] #1
- [X] `check-success=tests`
- [ ] `check-success=jenkins/fake-tests`
"""
    )

    state = await checks_status.get_rule_checks_status(
        mock.Mock(),
        FAKE_REPO,
        typing.cast(typing.List[context.BasePullRequest], pulls),
        mock.Mock(conditions=c),
    )
    assert state == check_api.Conclusion.FAILURE


async def assert_queue_rule_checks_status(
    conds: typing.List[typing.Collection[str]],
    pull: FakeQueuePullRequest,
    expected_state: checks_status.ChecksCombinedStatus,
) -> None:
    pull.sync_checks()
    schema = voluptuous.Schema(
        voluptuous.All(
            [voluptuous.Coerce(rules.RuleConditionSchema)],
            voluptuous.Coerce(conditions.QueueRuleConditions),
        )
    )

    c = schema(conds)

    await c([pull])
    state = await checks_status.get_rule_checks_status(
        mock.Mock(),
        FAKE_REPO,
        [typing.cast(context.BasePullRequest, pull)],
        mock.Mock(conditions=c),
        unmatched_conditions_return_failure=False,
        use_new_rule_checks_status=True,
    )
    assert state == expected_state


async def test_rules_checks_basic(logger_checker):
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "label": [],
            "check-success": [],
            "check-neutral": [],
            "check-failure": [],
            "check-pending": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = ["check-success=fake-ci", "label=foobar"]

    # Label missing and nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Label missing and success
    pull.attrs["check-success"] = ["fake-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # label ok and nothing reported
    pull.attrs["label"] = ["foobar"]
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["fake-ci"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = ["whatever"]
    pull.attrs["check-failure"] = ["foo", "fake-ci"]
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = ["whatever"]
    pull.attrs["check-failure"] = ["foo"]
    pull.attrs["check-success"] = ["fake-ci", "test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_with_and_or(logger_checker: None) -> None:
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "label": [],  # type: ignore
            "check-success": [],  # type: ignore
            "check-failure": [],  # type: ignore
            "check-pending": [],  # type: ignore
            "check-neutral": [],  # type: ignore
            "check-skipped": [],  # type: ignore
            "check-stale": [],  # type: ignore
        }
    )
    conds = [
        {"or": ["check-success=fake-ci", "label=skip-tests"]},
        "check-success=other-ci",
    ]

    # Label missing and nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Label missing and half success
    pull.attrs["check-success"] = ["fake-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Label missing and half success and half pending
    pull.attrs["check-success"] = ["fake-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Label missing and all success
    pull.attrs["check-success"] = ["fake-ci", "other-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # Label missing and half failure
    pull.attrs["check-success"] = ["fake-ci"]
    pull.attrs["check-failure"] = ["other-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Label missing and half failure bus
    pull.attrs["check-success"] = ["other-ci"]
    pull.attrs["check-failure"] = ["fake-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # label ok and nothing reported
    pull.attrs["label"] = ["skip-tests"]
    pull.attrs["check-success"] = []  # type: ignore
    pull.attrs["check-failure"] = []  # type: ignore
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # label ok and failure
    pull.attrs["label"] = ["skip-tests"]
    pull.attrs["check-success"] = []  # type: ignore
    pull.attrs["check-failure"] = ["other-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # label ok and failure
    pull.attrs["label"] = ["skip-tests"]
    pull.attrs["check-success"] = []  # type: ignore
    pull.attrs["check-failure"] = ["fake-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # label ok and success
    pull.attrs["label"] = ["skip-tests"]
    pull.attrs["check-pending"] = ["fake-ci"]
    pull.attrs["check-failure"] = []  # type: ignore
    pull.attrs["check-success"] = ["other-ci"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_status_with_negative_conditions1(logger_checker):
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = [
        "check-success=test-starter",
        "check-pending!=foo",
        "check-failure!=foo",
    ]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["foo"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["foo"]
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter", "foo"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported, sorry..., UNDEFINED BEHAVIOR
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_status_with_negative_conditions2():
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = [
        "check-success=test-starter",
        "-check-pending=foo",
        "-check-failure=foo",
    ]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["foo"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["foo"]
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter", "foo"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported, sorry..., UNDEFINED BEHAVIOR
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_status_with_negative_conditions3(logger_checker):
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = [
        "check-success=test-starter",
        "#check-pending=0",
        "#check-failure=0",
    ]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["foo"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["foo"]
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter", "foo"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported, sorry..., UNDEFINED BEHAVIOR
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["test-starter"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_status_with_or_conditions():
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = [
        {
            "or": ["check-success=ci-1", "check-success=ci-2"],
        }
    ]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["ci-1"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # Pending reported
    pull.attrs["check-pending"] = ["ci-1"]
    pull.attrs["check-failure"] = ["ci-2"]
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-1", "ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported success
    pull.attrs["check-failure"] = []
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = ["ci-1"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported failure, wait for ci-2 to finish
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)


async def test_rules_checks_status_expected_failure():
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = ["check-failure=ci-1"]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["ci-1"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # Success reported, no way!
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-1"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)


async def test_rules_checks_status_regular():
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = ["check-success=ci-1", "check-success=ci-2"]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["ci-1"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-1", "ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported success
    pull.attrs["check-failure"] = []
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = ["ci-1"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # half reported failure, fail early
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)


async def test_rules_checks_status_regex():
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],
            "check-failure": [],
            "check-pending": [],
            "check-neutral": [],
            "check-skipped": [],
            "check-stale": [],
        }
    )
    conds = ["check-success~=^ci-1$", "check-success~=^ci-2$"]

    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["ci-1"]
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-success"] = ["ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []
    pull.attrs["check-failure"] = []
    pull.attrs["check-success"] = ["ci-1", "ci-2"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)

    # half reported success
    pull.attrs["check-failure"] = []
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = ["ci-1"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # half reported failure, fail early
    pull.attrs["check-failure"] = ["ci-1"]
    pull.attrs["check-pending"] = []
    pull.attrs["check-success"] = []
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)


@freeze_time("2021-09-22T08:00:05", tz_offset=0)
async def test_rules_conditions_schedule():
    pulls = [
        FakeQueuePullRequest(
            {
                "number": 1,
                "author": "me",
                "base": "main",
                "current-timestamp": date.utcnow(),
                "current-time": date.utcnow(),
                "current-day": date.Day(22),
                "current-month": date.Month(9),
                "current-year": date.Year(2021),
                "current-day-of-week": date.DayOfWeek(3),
            }
        ),
    ]
    schema = voluptuous.Schema(
        voluptuous.All(
            [voluptuous.Coerce(rules.RuleConditionSchema)],
            voluptuous.Coerce(conditions.QueueRuleConditions),
        )
    )

    c = schema(
        [
            "base=main",
            "schedule=MON-FRI 08:00-17:00",
            "schedule=MONDAY-FRIDAY 10:00-12:00",
            "schedule=SAT-SUN 07:00-12:00",
        ]
    )

    await c(pulls)

    assert (
        c.get_summary()
        == """- [X] `base=main`
- [X] `schedule=MON-FRI 08:00-17:00`
- [ ] `schedule=MONDAY-FRIDAY 10:00-12:00`
- [ ] `schedule=SAT-SUN 07:00-12:00`
"""
    )


async def test_rules_checks_status_depop(logger_checker: None) -> None:
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "main",
            "head": "feature-1",
            "check-success": [],  # type: ignore
            "check-failure": [],  # type: ignore
            "check-pending": [],  # type: ignore
            "check-neutral": [],  # type: ignore
            "check-skipped": [],  # type: ignore
            "check-stale": [],  # type: ignore
            "approved-reviews-by": ["me"],
            "changes-requested-reviews-by": [],  # type: ignore
            "label": ["mergeit"],
        }
    )
    conds = [
        "check-success=Summary",
        "check-success=c-ci/status",
        "check-success=c-ci/s-c-t",
        "check-success=c-ci/c-p-validate",
        "#approved-reviews-by>=1",
        "approved-reviews-by=me",
        "-label=flag:wait",
        {
            "or": [
                "check-success=c-ci/status",
                "check-neutral=c-ci/status",
                "check-skipped=c-ci/status",
            ]
        },
        {
            "or": [
                "check-success=c-ci/s-c-t",
                "check-neutral=c-ci/s-c-t",
                "check-skipped=c-ci/s-c-t",
            ]
        },
        {
            "or": [
                "check-success=c-ci/c-p-validate",
                "check-neutral=c-ci/c-p-validate",
                "check-skipped=c-ci/c-p-validate",
            ]
        },
        "#approved-reviews-by>=1",
        "#changes-requested-reviews-by=0",
    ]
    # Nothing reported
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = []  # type: ignore
    pull.attrs["check-failure"] = []  # type: ignore
    pull.attrs["check-success"] = ["Summary", "continuous-integration/jenkins/pr-head"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = ["c-ci/status"]
    pull.attrs["check-failure"] = []  # type: ignore
    pull.attrs["check-success"] = [
        "Summary",
        "continuous-integration/jenkins/pr-head",
        "c-ci/s-c-t",
    ]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Pending reported
    pull.attrs["check-pending"] = [
        "c-ci/status",
        "c-ci/s-c-t",
        "c-ci/c-p-validate",
    ]
    pull.attrs["check-failure"] = ["c-ci/g-validate"]
    pull.attrs["check-success"] = ["Summary"]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.PENDING)

    # Failure reported
    pull.attrs["check-pending"] = []  # type: ignore
    pull.attrs["check-failure"] = [
        "c-ci/s-c-t",
        "c-ci/g-validate",
    ]
    pull.attrs["check-success"] = [
        "Summary",
        "c-ci/status",
        "c-ci/c-p-validate",
    ]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.FAILURE)

    # Success reported
    pull.attrs["check-pending"] = []  # type: ignore
    pull.attrs["check-failure"] = ["c-ci/g-validate"]
    pull.attrs["check-success"] = [
        "Summary",
        "c-ci/status",
        "c-ci/s-c-t",
        "c-ci/c-p-validate",
    ]
    await assert_queue_rule_checks_status(conds, pull, check_api.Conclusion.SUCCESS)


async def test_rules_checks_status_ceph(logger_checker: None) -> None:
    pull = FakeQueuePullRequest(
        {
            "number": 1,
            "current-year": date.Year(2018),
            "author": "me",
            "base": "devel",
            "head": "feature-1",
            "check-failure": [],  # type: ignore
            "check-neutral": [],  # type: ignore
            "check-skipped": [],  # type: ignore
            "check-stale": [],  # type: ignore
            "approved-reviews-by": ["me", "other"],
            "changes-requested-reviews-by": [],  # type: ignore
            "label": ["mergeit"],
            "check-success": ["Summary", "DCO", "build"],
            "check-success-or-neutral": ["Summary", "DCO", "build"],
            "check-success-or-neutral-or-pending": [
                "Summary",
                "DCO",
                "golangci-lint",
                "commitlint",
                "build",
                "codespell",
                "multi-arch-build",
                "go-test",
                "lint-extras",
                "mod-check",
                "go-test-api",
                "Rule: automatic merge (merge)",
                "ci/centos/mini-e2e/k8s-1.20",
                "ci/centos/upgrade-tests-rbd",
                "ci/centos/mini-e2e-helm/k8s-1.22",
                "ci/centos/upgrade-tests-cephfs",
                "ci/centos/mini-e2e/k8s-1.22",
                "ci/centos/k8s-e2e-external-storage/1.22",
                "ci/centos/mini-e2e-helm/k8s-1.21",
                "ci/centos/k8s-e2e-external-storage/1.21",
                "ci/centos/mini-e2e/k8s-1.21",
                "ci/centos/mini-e2e-helm/k8s-1.20",
            ],
            "status-success": ["Summary", "DCO", "build"],
            "check-pending": [
                "golangci-lint",
                "commitlint",
                "codespell",
                "multi-arch-build",
                "go-test",
                "lint-extras",
                "mod-check",
                "go-test-api",
                "Rule: automatic merge (merge)",
                "ci/centos/mini-e2e/k8s-1.20",
                "ci/centos/upgrade-tests-rbd",
                "ci/centos/mini-e2e-helm/k8s-1.22",
                "ci/centos/upgrade-tests-cephfs",
                "ci/centos/mini-e2e/k8s-1.22",
                "ci/centos/k8s-e2e-external-storage/1.22",
                "ci/centos/mini-e2e-helm/k8s-1.21",
                "ci/centos/k8s-e2e-external-storage/1.21",
                "ci/centos/mini-e2e/k8s-1.21",
                "ci/centos/mini-e2e-helm/k8s-1.20",
            ],
        }
    )
    pull.attrs["check"] = (
        pull.attrs.get("check-success", [])  # type: ignore
        + pull.attrs.get("check-neutral", [])  # type: ignore
        + pull.attrs.get("check-pending", [])  # type: ignore
        + pull.attrs.get("check-stale", [])  # type: ignore
        + pull.attrs.get("check-failure", [])  # type: ignore
        + pull.attrs.get("check-skipped", [])  # type: ignore
    )

    tree = {
        "and": [
            {"!=": ["label", "DNM"]},
            {"~=": ["base", "^(devel)|(release-.+)$"]},
            {">=": ["#approved-reviews-by", 2]},
            {"=": ["approved-reviews-by", "@ceph/ceph-csi-maintainers"]},
            {"=": ["#changes-requested-reviews-by", 0]},
            {"=": ["status-success", "codespell"]},
            {"=": ["status-success", "multi-arch-build"]},
            {"=": ["status-success", "go-test"]},
            {"=": ["status-success", "golangci-lint"]},
            {"=": ["status-success", "commitlint"]},
            {"=": ["status-success", "mod-check"]},
            {"=": ["status-success", "lint-extras"]},
            {"=": ["status-success", "ci/centos/k8s-e2e-external-storage/1.21"]},
            {"=": ["status-success", "ci/centos/k8s-e2e-external-storage/1.22"]},
            {"=": ["status-success", "ci/centos/mini-e2e-helm/k8s-1.20"]},
            {"=": ["status-success", "ci/centos/mini-e2e-helm/k8s-1.21"]},
            {"=": ["status-success", "ci/centos/mini-e2e-helm/k8s-1.22"]},
            {"=": ["status-success", "ci/centos/mini-e2e/k8s-1.20"]},
            {"=": ["status-success", "ci/centos/mini-e2e/k8s-1.21"]},
            {"=": ["status-success", "ci/centos/mini-e2e/k8s-1.22"]},
            {"=": ["status-success", "ci/centos/upgrade-tests-cephfs"]},
            {"=": ["status-success", "ci/centos/upgrade-tests-rbd"]},
            {"=": ["status-success", "DCO"]},
        ]
    }
    f = filter.IncompleteChecksFilter(
        typing.cast(filter.TreeT, tree),
        pending_checks=pull.attrs["check-pending"],  # type: ignore
        all_checks=pull.attrs["check"],  # type: ignore
    )

    async def fake_get_team_members(*args):
        return ["me", "other", "foo", "bar"]

    repo_with_team = mock.Mock(
        repo={"owner": {"login": "ceph"}},
        installation=mock.Mock(
            get_team_members=mock.Mock(side_effect=fake_get_team_members)
        ),
    )

    live_resolvers.configure_filter(repo_with_team, f)
    assert await f(pull) == filter.IncompleteCheck
