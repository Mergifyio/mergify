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
import typing

import pkg_resources
import voluptuous

from mergify_engine import check_api


global _ACTIONS_CLASSES
_ACTIONS_CLASSES = None


def get_classes():
    global _ACTIONS_CLASSES
    if _ACTIONS_CLASSES is None:
        _ACTIONS_CLASSES = dict(
            (ep.name, ep.load())
            for ep in pkg_resources.iter_entry_points("mergify_actions")
        )
    return _ACTIONS_CLASSES


def get_action_schemas():
    return dict(
        (name, obj.get_schema()) for name, obj in get_classes().items() if obj.is_action
    )


def get_commands():
    return dict((name, obj) for name, obj in get_classes().items() if obj.is_command)


@dataclasses.dataclass  # type: ignore
class Action(abc.ABC):
    is_action = True
    is_command = False

    always_run = False

    config: typing.Dict

    cancelled_check_report = check_api.Result(
        check_api.Conclusion.CANCELLED,
        "The rule doesn't match anymore",
        "This action has been cancelled.",
    )

    # If an action can't be twice in a rule this must be set to true
    only_once = False

    # If set to True, does not post the report to the Check API
    # Only keep it for internal tracking
    silent_report = False

    # This makes checks created by mergify retriggering Mergify, beware to
    # not create something that endup with a infinite loop of events
    allow_retrigger_mergify = False

    @property
    @staticmethod
    @abc.abstractmethod
    def validator():  # pragma: no cover
        pass

    @classmethod
    def get_schema(cls):
        return voluptuous.All(cls.validator, voluptuous.Coerce(cls))

    @staticmethod
    def command_to_config(string):  # pragma: no cover
        """Convert string to dict config"""
        return {}

    def run(
        self, ctxt, rule, missing_conditions
    ) -> check_api.Result:  # pragma: no cover
        pass

    def cancel(
        self, ctxt, rule, missing_conditions
    ) -> check_api.Result:  # pragma: no cover
        return self.cancelled_check_report
