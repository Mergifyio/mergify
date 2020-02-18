# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import attr
import pkg_resources
import voluptuous

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


@attr.s
class Action(abc.ABC):
    is_action = True
    is_command = False

    always_run = False

    config = attr.ib()
    cancelled_check_report = (
        "neutral",
        "The rule doesn't match anymore, this action has been cancelled",
        "",
    )

    # If an action can't be twice in a rule this must be set to true
    only_once = False

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

    @staticmethod
    def run(pull, sources, missing_conditions):  # pragma: no cover
        pass

    @staticmethod
    def cancel(pull, sources, missing_conditions):  # pragma: no cover
        pass
