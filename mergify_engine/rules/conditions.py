# -*- encoding: utf-8 -*-
#
# Copyright © 2018-2021 Mergify SAS
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
import textwrap
import typing

import daiquiri
import voluptuous

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.clients import http
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers
from mergify_engine.rules import parser


LOG = daiquiri.getLogger(__name__)


# This helps mypy breaking the recursive definition
FakeTreeT = typing.Dict[str, typing.Any]


@dataclasses.dataclass
class RuleCondition:
    """This describe a leaf of the `conditions:` tree, eg:

    label=foobar
    -merged
    """

    condition: typing.Union[str, FakeTreeT]
    label: typing.Optional[str] = None
    description: typing.Optional[str] = None
    partial_filter: filter.Filter[bool] = dataclasses.field(init=False)
    match: bool = dataclasses.field(init=False, default=False)
    _used: bool = dataclasses.field(init=False, default=False)
    evaluation_error: typing.Optional[str] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        self.update(self.condition)

    def update(self, condition_raw: typing.Union[str, FakeTreeT]) -> None:
        self.condition = condition_raw

        try:
            if isinstance(condition_raw, str):
                condition = parser.parse(condition_raw)
            else:
                condition = condition_raw
            self.partial_filter = filter.BinaryFilter(
                typing.cast(filter.TreeT, condition)
            )
        except (parser.ConditionParsingError, filter.InvalidQuery) as e:
            raise voluptuous.Invalid(
                message=f"Invalid condition '{condition_raw}'. {str(e)}",
                error_message=str(e),
            )

    def update_attribute_name(self, new_name: str) -> None:
        tree = typing.cast(filter.TreeT, self.partial_filter.tree)
        negate = "-" in tree
        tree = tree.get("-", tree)
        operator = list(tree.keys())[0]
        name, value = list(tree.values())[0]
        if name.startswith(filter.Filter.LENGTH_OPERATOR):
            new_name = f"{filter.Filter.LENGTH_OPERATOR}{new_name}"

        new_tree: FakeTreeT = {operator: (new_name, value)}
        if negate:
            new_tree = {"-": new_tree}
        self.update(new_tree)

    def __str__(self) -> str:
        if self.label is not None:
            return self.label
        elif isinstance(self.condition, str):
            return self.condition
        else:
            return str(self.partial_filter)

    def copy(self) -> "RuleCondition":
        rc = RuleCondition(self.condition, self.label, self.description)
        rc.partial_filter.value_expanders = self.partial_filter.value_expanders
        return rc

    async def __call__(self, obj: filter.GetAttrObjectT) -> bool:
        if self._used:
            raise RuntimeError("RuleCondition cannot be reused")
        self._used = True
        try:
            self.match = await self.partial_filter(obj)
        except live_resolvers.LiveResolutionFailure as e:
            self.match = False
            self.evaluation_error = e.reason
        return self.match

    def get_attribute_name(self) -> str:
        tree = typing.cast(filter.TreeT, self.partial_filter.tree)
        tree = tree.get("-", tree)
        name = list(tree.values())[0][0]
        if name.startswith(filter.Filter.LENGTH_OPERATOR):
            return str(name[1:])
        return str(name)


@dataclasses.dataclass
class RuleConditionGroup:
    """This describe a group leafs of the `conditions:` tree linked by and or or."""

    condition: dataclasses.InitVar[
        typing.Dict[
            typing.Literal["and", "or"],
            typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
        ]
    ]
    operator: typing.Literal["and", "or"] = dataclasses.field(init=False)
    conditions: typing.List[
        typing.Union["RuleConditionGroup", RuleCondition]
    ] = dataclasses.field(init=False)
    description: typing.Optional[str] = None
    match: bool = dataclasses.field(init=False, default=False)
    _used: bool = dataclasses.field(init=False, default=False)

    def __post_init__(
        self,
        condition: typing.Dict[
            typing.Literal["and", "or"],
            typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
        ],
    ) -> None:
        if len(condition) != 1:
            raise RuntimeError("Invalid condition")

        self.operator, self.conditions = next(iter(condition.items()))

    async def __call__(
        self,
        obj: filter.GetAttrObjectT,
    ) -> bool:
        if self._used:
            raise RuntimeError("RuleConditionGroup cannot be re-used")
        self._used = True
        self.match = await filter.BinaryFilter(
            typing.cast(
                filter.TreeT,
                {self.operator: self.conditions},
            )
        )(obj)
        return self.match

    def extract_raw_filter_tree(
        self,
        condition: typing.Optional[
            typing.Union["RuleConditionGroup", RuleCondition]
        ] = None,
    ) -> filter.TreeT:
        if condition is None:
            condition = self

        if isinstance(condition, RuleCondition):
            return typing.cast(filter.TreeT, condition.partial_filter.tree)
        elif isinstance(condition, RuleConditionGroup):
            return typing.cast(
                filter.TreeT,
                {
                    condition.operator: [
                        self.extract_raw_filter_tree(c) for c in condition.conditions
                    ]
                },
            )
        else:
            raise RuntimeError("unexpected condition instance")

    def walk(
        self,
        conditions: typing.Optional[
            typing.List[typing.Union["RuleConditionGroup", RuleCondition]]
        ] = None,
    ) -> typing.Iterator[RuleCondition]:
        if conditions is None:
            conditions = self.conditions
        for condition in conditions:
            if isinstance(condition, RuleCondition):
                yield condition
            elif isinstance(condition, RuleConditionGroup):
                for _condition in self.walk(condition.conditions):
                    yield _condition
            else:
                raise RuntimeError(f"Unsupported condition type: {type(condition)}")

    def is_faulty(self) -> bool:
        return any(c.evaluation_error for c in self.walk())

    def copy(self) -> "RuleConditionGroup":
        return self.__class__(
            {self.operator: [c.copy() for c in self.conditions]},
            description=self.description,
        )

    @staticmethod
    def _get_rule_condition_summary(cond: RuleCondition) -> str:
        summary = ""
        checked = "X" if cond.match else " "
        summary += f"- [{checked}] `{cond}`"
        if cond.description:
            summary += f" [{cond.description}]"
        if cond.evaluation_error:
            summary += f" ⚠️ {cond.evaluation_error}"
        summary += "\n"
        return summary

    @classmethod
    def _walk_for_summary(
        cls,
        conditions: typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
        level: int = 0,
    ) -> str:
        summary = ""
        for condition in conditions:
            if isinstance(condition, RuleCondition):
                summary += cls._get_rule_condition_summary(condition)
            elif isinstance(condition, RuleConditionGroup):
                label = "all of" if condition.operator == "and" else "any of"
                checked = "X" if condition.match else " "
                summary += f"- [{checked}] {label}:"
                if condition.description:
                    summary += f" [{condition.description}]"
                summary += "\n"
                for _sum in cls._walk_for_summary(condition.conditions, level + 1):
                    summary += _sum
            else:
                raise RuntimeError(f"Unsupported condition type: {type(condition)}")

        return textwrap.indent(summary, "  " * level)

    def get_summary(self) -> str:
        return self._walk_for_summary(self.conditions)


@dataclasses.dataclass
class QueueRuleConditions:
    conditions: dataclasses.InitVar[
        typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
    ]
    condition: RuleConditionGroup = dataclasses.field(init=False)
    _evaluated_conditions: typing.Dict[
        github_types.GitHubPullRequestNumber, RuleConditionGroup
    ] = dataclasses.field(default_factory=dict, init=False, repr=False)
    match: bool = dataclasses.field(init=False, default=False)
    _used: bool = dataclasses.field(init=False, default=False)

    def __post_init__(
        self,
        conditions: typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
    ) -> None:
        self.condition = RuleConditionGroup({"and": conditions})

    def copy(self) -> "QueueRuleConditions":
        return QueueRuleConditions(self.condition.copy().conditions)

    def extract_raw_filter_tree(self) -> filter.TreeT:
        return self.condition.extract_raw_filter_tree()

    async def __call__(
        self, pull_requests: typing.List[context.BasePullRequest]
    ) -> bool:
        if self._used:
            raise RuntimeError("QueueRuleConditions cannot be re-used")
        self._used = True

        for pull in pull_requests:
            c = self.condition.copy()
            await c(pull)
            self._evaluated_conditions[
                await pull.number  # type: ignore[attr-defined]
            ] = c

        self.match = all(c.match for c in self._evaluated_conditions.values())
        return self.match

    @classmethod
    def _get_rule_condition_summary(
        cls,
        conditions: typing.Mapping[github_types.GitHubPullRequestNumber, RuleCondition],
    ) -> str:

        first_key = next(iter(conditions))
        display_detail = (
            conditions[first_key].get_attribute_name()
            not in context.QueuePullRequest.QUEUE_ATTRIBUTES
        )

        summary = "- "
        if not display_detail:
            checked = "X" if conditions[first_key].match else " "
            summary += f"[{checked}] "
        summary += f"`{conditions[first_key]}`"

        if conditions[first_key].description:
            summary += f" [{conditions[first_key].description}]"
        summary += "\n"

        if display_detail:
            for pull_number, cond in conditions.items():
                checked = "X" if cond.match else " "
                summary += f"  - [{checked}] #{pull_number}"
                if cond.evaluation_error:
                    summary += f" ⚠️ {cond.evaluation_error}"
                summary += "\n"

        return summary

    @classmethod
    def _walk_for_summary(
        cls,
        evaluated_conditions: typing.Mapping[
            github_types.GitHubPullRequestNumber,
            typing.Union["RuleConditionGroup", "RuleCondition"],
        ],
        operator: typing.Literal["and", "or"],
        level: int = -1,
    ) -> str:
        summary = ""
        if not evaluated_conditions:
            raise RuntimeError("Empty conditions group")
        first_key = next(iter(evaluated_conditions))
        if isinstance(evaluated_conditions[first_key], RuleCondition):
            evaluated_conditions = typing.cast(
                typing.Mapping[
                    github_types.GitHubPullRequestNumber,
                    RuleCondition,
                ],
                evaluated_conditions,
            )
            summary += cls._get_rule_condition_summary(evaluated_conditions)
        elif isinstance(evaluated_conditions[first_key], RuleConditionGroup):
            evaluated_conditions = typing.cast(
                typing.Mapping[
                    github_types.GitHubPullRequestNumber,
                    RuleConditionGroup,
                ],
                evaluated_conditions,
            )
            if level >= 0:
                label = (
                    "all of"
                    if evaluated_conditions[first_key].operator == "and"
                    else "any of"
                )
                if evaluated_conditions[first_key].description:
                    label += f" [{evaluated_conditions[first_key].description}]"
                global_match = all(c.match for c in evaluated_conditions.values())
                checked = "X" if global_match else " "
                summary += f"- [{checked}] {label}:\n"

            inner_conditions = []
            for i in range(len(evaluated_conditions[first_key].conditions)):
                inner_conditions.append(
                    {p: c.conditions[i] for p, c in evaluated_conditions.items()}
                )
            for inner_condition in inner_conditions:
                for _sum in cls._walk_for_summary(
                    inner_condition,
                    evaluated_conditions[first_key].operator,
                    level + 1,
                ):
                    summary += _sum
        else:
            raise RuntimeError(
                f"Unsupported condition type: {type(evaluated_conditions[first_key])}"
            )

        return textwrap.indent(summary, "  " * level)

    def get_summary(self) -> str:
        if self._used:
            return self._walk_for_summary(self._evaluated_conditions, "and")
        else:
            return self.condition.get_summary()

    def is_faulty(self) -> bool:
        if self._used:
            return any(c.is_faulty() for c in self._evaluated_conditions.values())
        else:
            return self.condition.is_faulty()

    def walk(self) -> typing.Iterator[RuleCondition]:
        if self._used:
            for conditions in self._evaluated_conditions.values():
                for cond in conditions.walk():
                    yield cond
        else:
            for cond in self.condition.walk():
                yield cond


BRANCH_PROTECTION_CONDITION_TAG = "🛡 GitHub branch protection"


async def get_branch_protection_conditions(
    repository: context.Repository,
    ref: github_types.GitHubRefType,
    *,
    strict: bool,
) -> typing.List[typing.Union["RuleConditionGroup", RuleCondition]]:
    protection = await repository.get_branch_protection(ref)
    conditions: typing.List[typing.Union["RuleConditionGroup", RuleCondition]] = []
    if protection:
        if "required_status_checks" in protection:
            conditions.extend(
                [
                    RuleConditionGroup(
                        {
                            "or": [
                                RuleCondition(f"check-success={check}"),
                                RuleCondition(f"check-neutral={check}"),
                                RuleCondition(f"check-skipped={check}"),
                            ]
                        },
                        description=BRANCH_PROTECTION_CONDITION_TAG,
                    )
                    for check in protection["required_status_checks"]["contexts"]
                ]
            )
            if (
                strict
                and "strict" in protection["required_status_checks"]
                and protection["required_status_checks"]["strict"]
            ):
                conditions.append(
                    RuleCondition(
                        "#commits-behind=0",
                        description=BRANCH_PROTECTION_CONDITION_TAG,
                    )
                )

        if (
            "required_pull_request_reviews" in protection
            and protection["required_pull_request_reviews"][
                "required_approving_review_count"
            ]
            > 0
        ):
            conditions.extend(
                [
                    RuleCondition(
                        f"#approved-reviews-by>={protection['required_pull_request_reviews']['required_approving_review_count']}",
                        description=BRANCH_PROTECTION_CONDITION_TAG,
                    ),
                    RuleCondition(
                        "#changes-requested-reviews-by=0",
                        description=BRANCH_PROTECTION_CONDITION_TAG,
                    ),
                ]
            )

        if (
            "required_conversation_resolution" in protection
            and protection["required_conversation_resolution"]["enabled"]
        ):
            conditions.append(
                RuleCondition(
                    "#review-threads-unresolved=0",
                    description=BRANCH_PROTECTION_CONDITION_TAG,
                )
            )

    return conditions


async def get_depends_on_conditions(
    ctxt: context.Context,
) -> typing.List[typing.Union["RuleConditionGroup", RuleCondition]]:
    conds: typing.List[typing.Union["RuleConditionGroup", RuleCondition]] = []
    for pull_request_number in ctxt.get_depends_on():
        try:
            dep_ctxt = await ctxt.repository.get_pull_request_context(
                pull_request_number
            )
        except http.HTTPNotFound:
            description = f"⛓️ ⚠️ *pull request not found* (#{pull_request_number})"
        else:
            description = f"⛓️ **{dep_ctxt.pull['title']}** ([#{pull_request_number}]({dep_ctxt.pull['html_url']}))"
        conds.append(
            RuleCondition(
                {"=": ("depends-on", f"#{pull_request_number}")},
                description=description,
            )
        )
    return conds


@dataclasses.dataclass
class PullRequestRuleConditions:
    conditions: dataclasses.InitVar[
        typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
    ]
    condition: RuleConditionGroup = dataclasses.field(init=False)

    def __post_init__(
        self,
        conditions: typing.List[typing.Union["RuleConditionGroup", RuleCondition]],
    ) -> None:
        self.condition = RuleConditionGroup({"and": conditions})

    async def __call__(self, objs: typing.List[context.BasePullRequest]) -> bool:
        if len(objs) > 1:
            raise RuntimeError(
                "PullRequestRuleConditions take only one pull request at a time"
            )
        return await self.condition(objs[0])

    def extract_raw_filter_tree(self) -> filter.TreeT:
        return self.condition.extract_raw_filter_tree()

    def get_summary(self) -> str:
        return self.condition.get_summary()

    @property
    def match(self) -> bool:
        return self.condition.match

    def is_faulty(self) -> bool:
        return self.condition.is_faulty()

    def walk(self) -> typing.Iterator[RuleCondition]:
        for cond in self.condition.walk():
            yield cond

    def copy(self) -> "PullRequestRuleConditions":
        return PullRequestRuleConditions(self.condition.copy().conditions)
