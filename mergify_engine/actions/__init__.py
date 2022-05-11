# -*- encoding: utf-8 -*-
#
#  Copyright © 2018—2020 Mergify SAS
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


import abc
import dataclasses
import enum
import importlib.metadata
import typing

import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine.rules import conditions


CANCELLED_CHECK_REPORT = check_api.Result(
    check_api.Conclusion.CANCELLED,
    "The rule doesn't match anymore",
    "This action has been cancelled.",
)


global _ACTIONS_CLASSES
_ACTIONS_CLASSES: typing.Optional[typing.Dict[str, "Action"]] = None

RawConfigT = typing.Dict[str, typing.Any]


@enum.unique
class ActionFlag(enum.Flag):
    NONE = 0
    # Action can be used as pull_request_rules/actions
    ALLOW_AS_ACTION = enum.auto()
    # Action can be used as command
    ALLOW_AS_COMMAND = enum.auto()
    # The action run()/cancel() is executed whatever the previous state was
    ALWAYS_RUN = enum.auto()
    # The result of action will be posted as GitHub Check-run
    ALWAYS_SEND_REPORT = enum.auto()
    # The action can be run when the Mergify configuration change
    ALLOW_ON_CONFIGURATION_CHANGED = enum.auto()
    # Allow to rerun an action if it's part of another rule
    DISALLOW_RERUN_ON_OTHER_RULES = enum.auto()
    # This makes checks created by mergify retriggering Mergify, beware to
    # not create something that endup with a infinite loop of events
    ALLOW_RETRIGGER_MERGIFY = enum.auto()
    # Allow to return this command as pending if does not match its conditions
    # requirements.
    ALLOW_AS_PENDING_COMMAND = enum.auto()
    # Once succeed the action must not be ran anymore
    SUCCESS_IS_FINAL_STATE = enum.auto()


def get_classes() -> typing.Dict[str, "Action"]:
    global _ACTIONS_CLASSES
    if _ACTIONS_CLASSES is None:
        _ACTIONS_CLASSES = {
            ep.name: ep.load()
            for ep in importlib.metadata.entry_points(group="mergify_actions")
        }
    return _ACTIONS_CLASSES


def get_action_schemas(
    partial_validation: bool = False,
) -> typing.Dict[str, typing.Any]:
    return {
        name: obj.get_schema(partial_validation)
        for name, obj in get_classes().items()
        if ActionFlag.ALLOW_AS_ACTION in obj.flags
    }


def get_commands() -> typing.Dict[str, "Action"]:
    return {
        name: obj
        for name, obj in get_classes().items()
        if ActionFlag.ALLOW_AS_COMMAND in obj.flags
    }


@dataclasses.dataclass  # type: ignore[misc]
class Action(abc.ABC):
    # FIXME: this might be more precise if we replace voluptuous by pydantic somehow?
    config: RawConfigT
    raw_config: RawConfigT

    flags: typing.ClassVar[ActionFlag] = ActionFlag.NONE
    validator: typing.ClassVar[typing.Dict[typing.Any, typing.Any]]

    def validate_config(
        self, mergify_config: "rules.MergifyConfig"
    ) -> None:  # pragma: no cover
        pass

    @classmethod
    def get_schema(cls, partial_validation: bool = False) -> typing.Any:
        def schema(raw_config: typing.Optional[RawConfigT]) -> typing.Any:
            if raw_config is None:
                raw_config = {}
            schema = voluptuous.All(
                cls.get_config_schema(partial_validation),
                voluptuous.Coerce(lambda v: cls(v, raw_config)),
            )
            return voluptuous.Schema(schema)(raw_config)

        return schema

    @classmethod
    def get_config_schema(
        cls, partial_validation: bool
    ) -> typing.Dict[typing.Any, typing.Any]:
        return cls.validator

    @staticmethod
    def command_to_config(string: str) -> typing.Dict[str, typing.Any]:
        """Convert string to dict config"""
        return {}

    @abc.abstractmethod
    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        ...

    @abc.abstractmethod
    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        ...

    @staticmethod
    async def wanted_users(
        ctxt: context.Context, users: typing.List[str]
    ) -> typing.Set[str]:
        wanted = set()
        for user in set(users):
            try:
                user = await ctxt.pull_request.render_template(user)
            except context.RenderTemplateFailure:
                # NOTE: this should never happen since
                # the template is validated when parsing the config 🤷
                continue
            else:
                wanted.add(user)

        return wanted

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        return []
