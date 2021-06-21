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
import functools
import itertools
import operator
import textwrap
import typing

import daiquiri
import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.clients import http
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers
from mergify_engine.rules import parser
from mergify_engine.rules import types


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
    description: typing.Optional[str] = None
    partial_filter: filter.Filter[bool] = dataclasses.field(init=False)
    match: bool = dataclasses.field(init=False, default=False)
    _used: bool = dataclasses.field(init=False, default=False)
    evaluation_error: typing.Optional[str] = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        self.update(self.condition)

    def update(self, condition_raw: typing.Union[str, FakeTreeT]) -> None:
        try:
            if isinstance(condition_raw, str):
                condition = parser.search.parseString(condition_raw, parseAll=True)[0]
            else:
                condition = condition_raw
            self.partial_filter = filter.BinaryFilter(
                typing.cast(filter.TreeT, condition)
            )
        except (parser.pyparsing.ParseException, filter.InvalidQuery) as e:
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
        if isinstance(self.condition, str):
            return self.condition
        else:
            return str(self.partial_filter)

    def copy(self) -> "RuleCondition":
        return RuleCondition(self.condition, self.description)

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

    async def __call__(self, obj: filter.GetAttrObjectT) -> bool:
        if self._used:
            raise RuntimeError("RuleConditionGroup cannot be re-used")
        self._used = True
        self.match = await filter.BinaryFilter(
            typing.cast(filter.TreeT, {self.operator: self.conditions})
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
        return RuleConditionGroup(
            {self.operator: [c.copy() for c in self.conditions]},
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
                summary += f"- [{checked}] {label}:\n"
                for _sum in cls._walk_for_summary(condition.conditions, level + 1):
                    summary += _sum
            else:
                raise RuntimeError(f"Unsupported condition type: {type(condition)}")

        return textwrap.indent(summary, "  " * level)

    def get_summary(self) -> str:
        return self._walk_for_summary(self.conditions)


def RuleConditions(
    conditions: typing.List[typing.Union["RuleConditionGroup", RuleCondition]]
) -> RuleConditionGroup:
    return RuleConditionGroup({"and": conditions})


async def get_branch_protection_conditions(
    ctxt: context.Context,
) -> typing.List[typing.Union["RuleConditionGroup", RuleCondition]]:
    return [
        RuleCondition(
            f"check-success-or-neutral={check}",
            description="🛡 GitHub branch protection",
        )
        for check in await ctxt.repository.get_branch_protection_checks(
            ctxt.pull["base"]["ref"]
        )
    ]


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


class DisabledDict(typing.TypedDict):
    reason: str


# TODO(sileht): rename me PullRequestRule ?
@dataclasses.dataclass
class Rule:
    name: str
    disabled: typing.Union[DisabledDict, None]
    conditions: RuleConditionGroup
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    class T_from_dict_required(typing.TypedDict):
        name: str
        disabled: typing.Union[DisabledDict, None]
        conditions: RuleConditionGroup
        actions: typing.Dict[str, actions.Action]

    class T_from_dict(T_from_dict_required, total=False):
        hidden: bool

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "Rule":
        return cls(**d)


EvaluatedRule = typing.NewType("EvaluatedRule", Rule)


class QueueConfig(typing.TypedDict):
    priority: int
    speculative_checks: int


EvaluatedQueueRule = typing.NewType("EvaluatedQueueRule", "QueueRule")


QueueName = typing.NewType("QueueName", str)


@dataclasses.dataclass
class QueueRule:
    name: QueueName
    conditions: RuleConditionGroup
    config: QueueConfig

    class T_from_dict(QueueConfig, total=False):
        name: QueueName
        conditions: RuleConditionGroup

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "QueueRule":
        name = d.pop("name")
        conditions = d.pop("conditions")
        return cls(name, conditions, d)

    async def get_pull_request_rule(
        self,
        ctxt: context.Context,
        pull: context.BasePullRequest,
    ) -> EvaluatedQueueRule:
        branch_protection_conditions = await get_branch_protection_conditions(ctxt)
        queue_rule_with_branch_protection = QueueRule(
            self.name,
            RuleConditions(
                self.conditions.copy().conditions + branch_protection_conditions
            ),
            self.config,
        )
        queue_rules_evaluator = await QueuesRulesEvaluator.create(
            [queue_rule_with_branch_protection],
            ctxt,
            pull,
            False,
        )
        return queue_rules_evaluator.matching_rules[0]


T_Rule = typing.TypeVar("T_Rule", Rule, QueueRule)
T_EvaluatedRule = typing.TypeVar("T_EvaluatedRule", EvaluatedRule, EvaluatedQueueRule)


@dataclasses.dataclass
class GenericRulesEvaluator(typing.Generic[T_Rule, T_EvaluatedRule]):
    """A rules that matches a pull request."""

    # Fixed base attributes that are not considered when looking for the
    # next matching rules.
    BASE_ATTRIBUTES = (
        "head",
        "base",
        "author",
        "merged_by",
    )

    TEAM_ATTRIBUTES = (
        "author",
        "merged_by",
        "approved-reviews-by",
        "dismissed-reviews-by",
        "commented-reviews-by",
    )

    # The list of pull request rules to match against.
    rules: typing.List[T_Rule]

    # The rules matching the pull request.
    matching_rules: typing.List[T_EvaluatedRule] = dataclasses.field(
        init=False, default_factory=list
    )

    # The rules that can't be computed due to runtime error (eg: team resolution failure)
    faulty_rules: typing.List[T_EvaluatedRule] = dataclasses.field(
        init=False, default_factory=list
    )

    # The rules not matching the pull request.
    ignored_rules: typing.List[T_EvaluatedRule] = dataclasses.field(
        init=False, default_factory=list
    )

    @classmethod
    async def create(
        cls,
        rules: typing.List[T_Rule],
        ctxt: context.Context,
        pull: context.BasePullRequest,
        hide_rule: bool,
    ) -> "GenericRulesEvaluator[T_Rule, T_EvaluatedRule]":
        self = cls(rules)

        for rule in self.rules:
            for attrib in self.TEAM_ATTRIBUTES:
                for condition in rule.conditions.walk():
                    condition.partial_filter.value_expanders[
                        attrib
                    ] = functools.partial(  # type: ignore[assignment]
                        live_resolvers.teams, ctxt
                    )

            await rule.conditions(pull)

            # NOTE(sileht):
            # In the summary, we display rules in four groups:
            # * rules where all attributes match -> matching_rules
            # * rules where only BASE_ATTRIBUTES match (filter out rule written for other branches) -> matching_rules
            # * rules that won't work due to a configuration issue detected at runtime (eg team doesn't exists) -> faulty_rules
            # * rules where only BASE_ATTRIBUTES don't match (mainly to hide rule written for other branches) -> ignored_rules
            categorized = False

            if hide_rule and not rule.conditions.match:
                # NOTE(sileht): Replace non-base attribute by true, if it
                # still matches it's a potential rule otherwise hide it.
                base_conditions = rule.conditions.copy()
                for condition in base_conditions.walk():
                    attr = condition.get_attribute_name()
                    if attr not in self.BASE_ATTRIBUTES:
                        condition.update("number>0")

                await base_conditions(pull)

                if not base_conditions.match:
                    self.ignored_rules.append(typing.cast(T_EvaluatedRule, rule))
                    categorized = True

                if not categorized and rule.conditions.is_faulty():
                    self.faulty_rules.append(typing.cast(T_EvaluatedRule, rule))
                    categorized = True

            if not categorized:
                self.matching_rules.append(typing.cast(T_EvaluatedRule, rule))

        return self


RulesEvaluator = GenericRulesEvaluator[Rule, EvaluatedRule]
QueuesRulesEvaluator = GenericRulesEvaluator[QueueRule, EvaluatedQueueRule]


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List[Rule]

    def __post_init__(self):
        # NOTE(sileht): Make sure each rule has a unique name because they are
        # used to serialize the rule/action result in summary. And the summary
        # uses as unique key something like: f"{rule.name} ({action.name})"
        sorted_rules = sorted(self.rules, key=operator.attrgetter("name"))
        grouped_rules = itertools.groupby(sorted_rules, operator.attrgetter("name"))
        for _, sub_rules in grouped_rules:
            sub_rules = list(sub_rules)
            if len(sub_rules) == 1:
                continue
            for n, rule in enumerate(sub_rules):
                rule.name += f" #{n + 1}"

    def __iter__(self):
        return iter(self.rules)

    def has_user_rules(self) -> bool:
        return any(rule for rule in self.rules if not rule.hidden)

    async def get_pull_request_rule(self, ctxt: context.Context) -> RulesEvaluator:
        runtime_rules = []
        for rule in self.rules:
            if not rule.actions:
                runtime_rules.append(
                    Rule(
                        name=rule.name,
                        disabled=rule.disabled,
                        conditions=rule.conditions.copy(),
                        actions=rule.actions,
                        hidden=rule.hidden,
                    )
                )
                continue

            actions_with_special_rules = {}
            actions_without_special_rules = {}
            for name, action in rule.actions.items():
                if name in ["queue", "merge"]:
                    actions_with_special_rules[name] = action
                else:
                    actions_without_special_rules[name] = action

            if actions_with_special_rules:
                branch_protection_conditions = await get_branch_protection_conditions(
                    ctxt
                )
                depends_on_conditions = await get_depends_on_conditions(ctxt)
                if branch_protection_conditions or depends_on_conditions:
                    runtime_rules.append(
                        Rule(
                            name=rule.name,
                            disabled=rule.disabled,
                            conditions=RuleConditions(
                                rule.conditions.copy().conditions
                                + branch_protection_conditions
                                + depends_on_conditions
                            ),
                            actions=actions_with_special_rules,
                            hidden=rule.hidden,
                        )
                    )
                else:
                    actions_without_special_rules.update(actions_with_special_rules)

            if actions_without_special_rules:
                runtime_rules.append(
                    Rule(
                        name=rule.name,
                        disabled=rule.disabled,
                        conditions=rule.conditions.copy(),
                        actions=actions_without_special_rules,
                        hidden=rule.hidden,
                    )
                )

        return await RulesEvaluator.create(runtime_rules, ctxt, ctxt.pull_request, True)


@dataclasses.dataclass
class QueueRules:
    rules: typing.List[QueueRule]

    def __iter__(self):
        return iter(self.rules)

    def __getitem__(self, key):
        for rule in self:
            if rule.name == key:
                return rule
        raise KeyError(f"{key} not found")

    def __len__(self):
        return len(self.rules)

    def __post_init__(self):
        names = set()
        for i, rule in enumerate(reversed(self.rules)):
            rule.config["priority"] = i
            if rule.name is names:
                raise voluptuous.error.Invalid(
                    f"queue_rules names must be unique, found `{rule.name}` twice"
                )
            names.add(rule.name)


class YAMLInvalid(voluptuous.Invalid):  # type: ignore[misc]
    def __str__(self):
        return f"{self.msg} at {self.path}"

    def get_annotations(self, path: str) -> typing.List[github_types.GitHubAnnotation]:
        if self.path:
            error_path = self.path[0]
            return [
                {
                    "path": path,
                    "start_line": error_path.line,
                    "end_line": error_path.line,
                    "start_column": error_path.column,
                    "end_column": error_path.column,
                    "annotation_level": "failure",
                    "message": self.error_message,
                    "title": self.msg,
                },
            ]
        return []


def YAML(v: bytes) -> typing.Any:
    try:
        return yaml.safe_load(v)
    except yaml.MarkedYAMLError as e:
        error_message = str(e)
        path = []
        if e.problem_mark is not None:
            path.append(
                types.LineColumnPath(e.problem_mark.line + 1, e.problem_mark.column + 1)
            )
        raise YAMLInvalid(
            message="Invalid YAML", error_message=error_message, path=path
        )
    except yaml.YAMLError as e:
        error_message = str(e)
        raise YAMLInvalid(message="Invalid YAML", error_message=error_message)


def RuleConditionSchema(v: typing.Any, depth: int = 0) -> typing.Any:
    if depth > 3:
        raise voluptuous.Invalid("Maximun number of nested conditions reached")

    return voluptuous.Schema(
        voluptuous.Any(
            voluptuous.All(str, voluptuous.Coerce(RuleCondition)),
            voluptuous.All(
                {
                    "and": voluptuous.All(
                        [lambda v: RuleConditionSchema(v, depth + 1)],
                        voluptuous.Length(min=2),
                    ),
                    "or": voluptuous.All(
                        [lambda v: RuleConditionSchema(v, depth + 1)],
                        voluptuous.Length(min=2),
                    ),
                },
                voluptuous.Length(min=1, max=1),
                voluptuous.Coerce(RuleConditionGroup),
            ),
        )
    )(v)


def get_pull_request_rules_schema(partial_validation: bool = False) -> voluptuous.All:
    return voluptuous.All(
        [
            voluptuous.All(
                {
                    voluptuous.Required("name"): str,
                    voluptuous.Required("disabled", default=None): voluptuous.Any(
                        None, {voluptuous.Required("reason"): str}
                    ),
                    voluptuous.Required("hidden", default=False): bool,
                    voluptuous.Required("conditions"): voluptuous.All(
                        [voluptuous.Coerce(RuleConditionSchema)],
                        voluptuous.Coerce(RuleConditions),
                    ),
                    voluptuous.Required("actions"): actions.get_action_schemas(
                        partial_validation
                    ),
                },
                voluptuous.Coerce(Rule.from_dict),
            ),
        ],
        voluptuous.Coerce(PullRequestRules),
    )


QueueRulesSchema = voluptuous.All(
    [
        voluptuous.All(
            {
                voluptuous.Required("name"): str,
                voluptuous.Required("conditions"): voluptuous.All(
                    [voluptuous.Coerce(RuleConditionSchema)],
                    voluptuous.Coerce(RuleConditions),
                ),
                voluptuous.Required("speculative_checks", default=1): voluptuous.All(
                    int, voluptuous.Range(min=1, max=20)
                ),
            },
            voluptuous.Coerce(QueueRule.from_dict),
        )
    ],
    voluptuous.Coerce(QueueRules),
)


def get_defaults_schema(
    partial_validation: bool,
) -> typing.Dict[typing.Any, typing.Any]:
    return {
        # FIXME(sileht): actions.get_action_schemas() returns only actions Actions
        # and not command only, since only refresh is command only and it doesn't
        # have options it's not a big deal.
        voluptuous.Required("actions", default={}): actions.get_action_schemas(
            partial_validation
        ),
    }


def FullifyPullRequestRules(v):
    try:
        for pr_rule in v["pull_request_rules"]:
            for action in pr_rule.actions.values():
                action.validate_config(v)
    except voluptuous.error.Error:
        raise
    except Exception as e:
        LOG.error("fail to dispatch config", exc_info=True)
        raise voluptuous.error.Invalid(str(e))
    return v


def UserConfigurationSchema(
    config: typing.Dict[str, typing.Any], partial_validation: bool = False
) -> voluptuous.Schema:
    schema = {
        voluptuous.Required(
            "pull_request_rules", default=[]
        ): get_pull_request_rules_schema(partial_validation),
        voluptuous.Required("queue_rules", default=[]): QueueRulesSchema,
        voluptuous.Required("defaults", default={}): get_defaults_schema(
            partial_validation
        ),
    }

    if not partial_validation:
        schema = voluptuous.And(schema, voluptuous.Coerce(FullifyPullRequestRules))

    return voluptuous.Schema(schema)(config)


YamlSchema = voluptuous.Schema(voluptuous.Coerce(YAML))


@dataclasses.dataclass
class InvalidRules(Exception):
    error: voluptuous.Invalid
    filename: str

    @staticmethod
    def _format_path_item(path_item):
        if isinstance(path_item, int):
            return f"item {path_item}"
        return str(path_item)

    def _format_error(self, error):
        msg = str(error.msg)

        if error.error_type:
            msg += f" for {error.error_type}"

        if error.path:
            path = " → ".join(map(self._format_path_item, error.path))
            msg += f" @ {path}"
        # Only include the error message if it has been provided
        # voluptuous set it to the `message` otherwise
        if error.error_message != error.msg:
            msg += f"\n```\n{error.error_message}\n```"
        return msg

    @classmethod
    def _walk_error(cls, root_error):
        if isinstance(root_error, voluptuous.MultipleInvalid):
            for error1 in root_error.errors:
                for error2 in cls._walk_error(error1):
                    yield error2
        else:
            yield root_error

    @property
    def errors(self):
        return list(self._walk_error(self.error))

    def __str__(self):
        if len(self.errors) >= 2:
            return "* " + "\n* ".join(sorted(map(self._format_error, self.errors)))
        return self._format_error(self.errors[0])

    def get_annotations(self, path: str) -> typing.List[github_types.GitHubAnnotation]:
        return functools.reduce(
            operator.add,
            (
                error.get_annotations(path)
                for error in self.errors
                if hasattr(error, "get_annotations")
            ),
            [],
        )


class Defaults(typing.TypedDict):
    actions: typing.Dict[str, actions.ActionSchema]


class MergifyConfig(typing.TypedDict):
    pull_request_rules: PullRequestRules
    queue_rules: QueueRules
    defaults: Defaults
    raw: typing.Dict[str, typing.Any]


def merge_config(config: typing.Dict[str, typing.Any]) -> typing.Dict[str, typing.Any]:
    if defaults := config.get("defaults"):
        if defaults_actions := defaults.get("actions"):
            for rule in config.get("pull_request_rules", []):
                actions = rule["actions"]

                for action_name, action in actions.items():
                    if action_name not in defaults_actions:
                        continue
                    elif defaults_actions[action_name] is None:
                        continue

                    if action is None:
                        rule["actions"][action_name] = defaults_actions[action_name]
                    else:
                        merged_action = defaults_actions[action_name] | action
                        rule["actions"][action_name].update(merged_action)
    return config


def get_mergify_config(
    config_file: context.MergifyConfigFile,
) -> MergifyConfig:
    try:
        config = YamlSchema(config_file["decoded_content"])
    except voluptuous.Invalid as e:
        raise InvalidRules(e, config_file["path"])

    # Allow an empty file
    if config is None:
        config = {}

    try:
        UserConfigurationSchema(config, partial_validation=True)
    except voluptuous.Invalid as e:
        raise InvalidRules(e, config_file["path"])

    merged_config = merge_config(config)

    try:
        config = UserConfigurationSchema(merged_config, partial_validation=False)
        config["raw"] = merged_config
        return typing.cast(MergifyConfig, config)
    except voluptuous.Invalid as e:
        raise InvalidRules(e, config_file["path"])
