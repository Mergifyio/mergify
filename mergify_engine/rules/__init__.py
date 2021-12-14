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
import logging
import operator
import typing

import daiquiri
import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.rules import conditions
from mergify_engine.rules import live_resolvers
from mergify_engine.rules import types


LOG = daiquiri.getLogger(__name__)


class DisabledDict(typing.TypedDict):
    reason: str


@dataclasses.dataclass
class PullRequestRule:
    name: str
    disabled: typing.Union[DisabledDict, None]
    conditions: conditions.PullRequestRuleConditions
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    class T_from_dict_required(typing.TypedDict):
        name: str
        disabled: typing.Union[DisabledDict, None]
        conditions: conditions.PullRequestRuleConditions
        actions: typing.Dict[str, actions.Action]

    class T_from_dict(T_from_dict_required, total=False):
        hidden: bool

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "PullRequestRule":
        return cls(**d)

    def get_check_name(self, action: str) -> str:
        return f"Rule: {self.name} ({action})"


EvaluatedRule = typing.NewType("EvaluatedRule", PullRequestRule)


class QueueConfig(typing.TypedDict):
    priority: int
    speculative_checks: int
    batch_size: int
    allow_inplace_speculative_checks: bool


EvaluatedQueueRule = typing.NewType("EvaluatedQueueRule", "QueueRule")


QueueName = typing.NewType("QueueName", str)


@dataclasses.dataclass
class QueueRule:
    name: QueueName
    conditions: conditions.QueueRuleConditions
    config: QueueConfig

    class T_from_dict(QueueConfig, total=False):
        name: QueueName
        conditions: conditions.QueueRuleConditions

    @classmethod
    def from_dict(cls, d: T_from_dict) -> "QueueRule":
        name = d.pop("name")
        conditions = d.pop("conditions")
        return cls(name, conditions, d)

    async def get_pull_request_rule(
        self,
        repository: context.Repository,
        ref: github_types.GitHubRefType,
        pulls: typing.List[context.BasePullRequest],
        logger: logging.LoggerAdapter,
        log_schedule_details: bool,
    ) -> EvaluatedQueueRule:
        branch_protection_conditions = (
            await conditions.get_branch_protection_conditions(repository, ref)
        )
        queue_rule_with_branch_protection = QueueRule(
            self.name,
            conditions.QueueRuleConditions(
                self.conditions.condition.copy().conditions
                + branch_protection_conditions
            ),
            self.config,
        )
        queue_rules_evaluator = await QueuesRulesEvaluator.create(
            [queue_rule_with_branch_protection],
            repository,
            pulls,
            False,
            logger,
            log_schedule_details,
        )
        return queue_rules_evaluator.matching_rules[0]


T_Rule = typing.TypeVar("T_Rule", PullRequestRule, QueueRule)
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
        repository: context.Repository,
        pulls: typing.List[context.BasePullRequest],
        hide_rule: bool,
        logger: logging.LoggerAdapter,
        log_schedule_details: bool,
    ) -> "GenericRulesEvaluator[T_Rule, T_EvaluatedRule]":
        self = cls(rules)

        for rule in self.rules:
            for condition in rule.conditions.walk():
                live_resolvers.configure_filter(repository, condition.partial_filter)

            await rule.conditions(pulls)

            if log_schedule_details:
                for condition in rule.conditions.walk():
                    condition_raw = str(condition)
                    if condition_raw.startswith("schedule"):
                        # FIXME(sileht): mixed async dict/list comprehension is bugged before 3.11
                        details = []
                        for pull in pulls:
                            details.append(
                                {
                                    attr: await getattr(pull, attr)
                                    for attr in (
                                        "number",
                                        "current-time",
                                        "current-day",
                                        "current-month",
                                        "current-year",
                                        "current-day-of-week",
                                    )
                                }
                            )

                        logger.info(
                            "delayed pull request refresh, schedule details",
                            condition=condition_raw,
                            match=condition.match,
                            tree=condition.partial_filter.tree,
                            pulls=details,
                        )

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

                await base_conditions(pulls)

                if not base_conditions.match:
                    self.ignored_rules.append(typing.cast(T_EvaluatedRule, rule))
                    categorized = True

                if not categorized and rule.conditions.is_faulty():
                    self.faulty_rules.append(typing.cast(T_EvaluatedRule, rule))
                    categorized = True

            if not categorized:
                self.matching_rules.append(typing.cast(T_EvaluatedRule, rule))

        return self


RulesEvaluator = GenericRulesEvaluator[PullRequestRule, EvaluatedRule]
QueuesRulesEvaluator = GenericRulesEvaluator[QueueRule, EvaluatedQueueRule]


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List[PullRequestRule]

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

    @staticmethod
    def _gen_rule_from(
        rule: PullRequestRule,
        new_actions: typing.Dict[str, actions.Action],
        extra_conditions: typing.List[
            typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
        ],
    ) -> PullRequestRule:
        return PullRequestRule(
            name=rule.name,
            disabled=rule.disabled,
            conditions=conditions.PullRequestRuleConditions(
                rule.conditions.condition.copy().conditions + extra_conditions
            ),
            actions=new_actions,
            hidden=rule.hidden,
        )

    async def get_pull_request_rule(self, ctxt: context.Context) -> RulesEvaluator:
        runtime_rules = []
        for rule in self.rules:
            if not rule.actions:
                runtime_rules.append(self._gen_rule_from(rule, rule.actions, []))
                continue

            actions_without_special_rules = {}
            for name, action in rule.actions.items():
                conditions = await action.get_conditions_requirements(ctxt)
                if conditions:
                    runtime_rules.append(
                        self._gen_rule_from(rule, {name: action}, conditions)
                    )
                else:
                    actions_without_special_rules[name] = action

            if actions_without_special_rules:
                runtime_rules.append(
                    self._gen_rule_from(rule, actions_without_special_rules, [])
                )

        return await RulesEvaluator.create(
            runtime_rules,
            ctxt.repository,
            [ctxt.pull_request],
            True,
            ctxt.log,
            ctxt.has_been_refreshed_by_timer(),
        )


@dataclasses.dataclass
class QueueRules:
    rules: typing.List[QueueRule]

    def __iter__(self) -> typing.Iterator[QueueRule]:
        return iter(self.rules)

    def __getitem__(self, key: QueueName) -> QueueRule:
        for rule in self:
            if rule.name == key:
                return rule
        raise KeyError(f"{key} not found")

    def get(self, key: QueueName) -> typing.Optional[QueueRule]:
        try:
            return self[key]
        except KeyError:
            return None

    def __len__(self) -> int:
        return len(self.rules)

    def __post_init__(self) -> None:
        names: typing.Set[QueueName] = set()
        for i, rule in enumerate(reversed(self.rules)):
            rule.config["priority"] = i
            if rule.name in names:
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
    if depth > 4:
        raise voluptuous.Invalid("Maximun number of nested conditions reached")

    return voluptuous.Schema(
        voluptuous.Any(
            voluptuous.All(str, voluptuous.Coerce(conditions.RuleCondition)),
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
                voluptuous.Coerce(conditions.RuleConditionGroup),
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
                        voluptuous.Coerce(conditions.PullRequestRuleConditions),
                    ),
                    voluptuous.Required("actions"): actions.get_action_schemas(
                        partial_validation
                    ),
                },
                voluptuous.Coerce(PullRequestRule.from_dict),
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
                    voluptuous.Coerce(conditions.QueueRuleConditions),
                ),
                voluptuous.Required("speculative_checks", default=1): voluptuous.All(
                    int, voluptuous.Range(min=1, max=20)
                ),
                voluptuous.Required("batch_size", default=1): voluptuous.All(
                    int, voluptuous.Range(min=1, max=20)
                ),
                voluptuous.Required(
                    "allow_inplace_speculative_checks", default=True
                ): bool,
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
    actions: typing.Dict[str, typing.Any]


class MergifyConfig(typing.TypedDict):
    pull_request_rules: PullRequestRules
    queue_rules: QueueRules
    defaults: Defaults


def merge_config(
    config: typing.Dict[str, typing.Any], defaults: typing.Dict[str, typing.Any]
) -> typing.Dict[str, typing.Any]:
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

    # Validate defaults
    try:
        UserConfigurationSchema(config, partial_validation=True)
    except voluptuous.Invalid as e:
        raise InvalidRules(e, config_file["path"])

    defaults = config.pop("defaults", {})
    merged_config = merge_config(config, defaults)

    try:
        config = UserConfigurationSchema(merged_config, partial_validation=False)
        config["defaults"] = defaults
        return typing.cast(MergifyConfig, config)
    except voluptuous.Invalid as e:
        raise InvalidRules(e, config_file["path"])
