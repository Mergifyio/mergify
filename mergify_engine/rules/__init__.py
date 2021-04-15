# -*- encoding: utf-8 -*-
#
# Copyright © 2018-2020 Mergify SAS
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
import typing

import daiquiri
import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers
from mergify_engine.rules import types


LOG = daiquiri.getLogger(__name__)


def RuleCondition(value: str) -> filter.Filter:
    try:
        return filter.Filter.parse(value)
    except filter.parser.pyparsing.ParseException as e:
        raise voluptuous.Invalid(
            message=f"Invalid condition '{value}'. {str(e)}", error_message=str(e)
        )
    except filter.InvalidQuery as e:
        raise voluptuous.Invalid(
            message=f"Invalid condition '{value}'. {str(e)}", error_message=str(e)
        )


RuleConditions = typing.NewType("RuleConditions", typing.List[filter.Filter])
RuleMissingConditions = typing.NewType(
    "RuleMissingConditions", typing.List[filter.Filter]
)


# TODO(sileht): rename me PullRequestRule ?
@dataclasses.dataclass
class Rule:
    name: str
    conditions: RuleConditions
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    class T_from_dict_required(typing.TypedDict):
        name: str
        conditions: RuleConditions
        actions: typing.Dict[str, actions.Action]

    class T_from_dict(T_from_dict_required, total=False):
        hidden: bool

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "Rule":
        return cls(**d)


# TODO(sileht): rename me EvaluatedPullRequestRule ?
@dataclasses.dataclass
class EvaluatedRule:
    name: str
    conditions: RuleConditions
    missing_conditions: RuleMissingConditions
    actions: typing.Dict[str, actions.Action]
    hidden: bool
    errors: typing.List[str]

    @classmethod
    def from_rule(
        cls,
        rule: "Rule",
        missing_conditions: RuleMissingConditions,
        errors: typing.List[str],
    ) -> "EvaluatedRule":
        return cls(
            rule.name,
            rule.conditions,
            missing_conditions,
            rule.actions,
            rule.hidden,
            errors or [],
        )


class QueueConfig(typing.TypedDict):
    priority: int
    speculative_checks: int


@dataclasses.dataclass
class EvaluatedQueueRule:
    name: str
    conditions: RuleConditions
    missing_conditions: RuleMissingConditions
    config: QueueConfig
    errors: typing.List[str]

    @classmethod
    def from_rule(
        cls,
        rule: "QueueRule",
        missing_conditions: RuleMissingConditions,
        errors: typing.List[str],
    ) -> "EvaluatedQueueRule":
        return cls(
            rule.name,
            rule.conditions,
            missing_conditions,
            rule.config,
            errors,
        )


QueueName = typing.NewType("QueueName", str)


@dataclasses.dataclass
class QueueRule:
    name: QueueName
    conditions: RuleConditions
    config: QueueConfig

    class T_from_dict(QueueConfig, total=False):
        name: QueueName
        conditions: RuleConditions

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "QueueRule":
        name = d.pop("name")
        conditions = d.pop("conditions")
        return cls(name, conditions, d)

    async def get_pull_request_rule(self, ctxt: context.Context) -> EvaluatedQueueRule:
        queue_rules_evaluator = await QueuesRulesEvaluator.create(
            [self], ctxt, EvaluatedQueueRule.from_rule, False
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
        rule_to_evaluated_rule_method: typing.Callable[
            [T_Rule, RuleMissingConditions, typing.List[str]],
            T_EvaluatedRule,
        ],
        hide_rule: bool,
    ) -> "GenericRulesEvaluator[T_Rule, T_EvaluatedRule]":
        self = cls(rules)

        for rule in self.rules:
            ignore_rules = False
            faulty_rules_errors = []
            next_conditions_to_validate = []
            for condition in rule.conditions:
                for attrib in self.TEAM_ATTRIBUTES:
                    # mypy thinks can take only Callable and not Coroutine
                    condition.value_expanders[attrib] = functools.partial(  # type: ignore
                        live_resolvers.teams, ctxt
                    )
                try:
                    pull_request_match = await condition(ctxt.pull_request)
                except live_resolvers.LiveResolutionFailure as e:
                    pull_request_match = False
                    faulty_rules_errors.append(e.reason)

                if not pull_request_match:
                    next_conditions_to_validate.append(condition)
                    if condition.attribute_name in self.BASE_ATTRIBUTES:
                        ignore_rules = True

            if faulty_rules_errors and hide_rule:
                self.faulty_rules.append(
                    rule_to_evaluated_rule_method(
                        rule,
                        RuleMissingConditions(next_conditions_to_validate),
                        faulty_rules_errors,
                    ),
                )
            elif ignore_rules and hide_rule:
                self.ignored_rules.append(
                    rule_to_evaluated_rule_method(
                        rule, RuleMissingConditions(next_conditions_to_validate), []
                    )
                )
            else:
                self.matching_rules.append(
                    rule_to_evaluated_rule_method(
                        rule, RuleMissingConditions(next_conditions_to_validate), []
                    )
                )
        return self


RulesEvaluator = GenericRulesEvaluator[Rule, EvaluatedRule]
QueuesRulesEvaluator = GenericRulesEvaluator[QueueRule, EvaluatedQueueRule]


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List[Rule]

    def __post_init__(self):
        # Make sure each rule has a unique name
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

    def has_user_rules(self):
        return any(rule for rule in self.rules if not rule.hidden)

    async def get_pull_request_rule(self, ctxt: context.Context) -> RulesEvaluator:
        return await RulesEvaluator.create(
            self.rules, ctxt, EvaluatedRule.from_rule, True
        )


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

    def get_annotations(self, path):
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
        path = [
            types.LineColumnPath(e.problem_mark.line + 1, e.problem_mark.column + 1)
        ]
        raise YAMLInvalid(
            message="Invalid YAML", error_message=error_message, path=path
        )
    except yaml.YAMLError as e:
        error_message = str(e)
        raise YAMLInvalid(message="Invalid YAML", error_message=error_message)


def get_pull_request_rules_schema(partial_validation: bool = False) -> voluptuous.All:
    return voluptuous.All(
        [
            voluptuous.All(
                {
                    voluptuous.Required("name"): str,
                    voluptuous.Required("hidden", default=False): bool,
                    voluptuous.Required("conditions"): [
                        voluptuous.All(str, voluptuous.Coerce(RuleCondition))
                    ],
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
                voluptuous.Required("conditions"): [
                    voluptuous.All(str, voluptuous.Coerce(RuleCondition))
                ],
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

    @property
    def errors(self):
        if isinstance(self.error, voluptuous.MultipleInvalid):
            return self.error.errors
        return [self.error]

    def __str__(self):
        if len(self.errors) >= 2:
            return "* " + "\n* ".join(sorted(map(self._format_error, self.errors)))
        return self._format_error(self.errors[0])

    def get_annotations(self, path):
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
