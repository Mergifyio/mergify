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

import base64
import dataclasses
import itertools
import operator
import typing

import httpx
import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.rules import filter


def PullRequestRuleCondition(value):
    try:
        return filter.Filter.parse(value)
    except filter.parser.pyparsing.ParseException as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)), error_message=str(e)
        )
    except filter.InvalidQuery as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)), error_message=str(e)
        )


PullRequestRulesSchema = voluptuous.Schema(
    voluptuous.All(
        [
            {
                voluptuous.Required("name"): str,
                voluptuous.Required("hidden", default=False): bool,
                voluptuous.Required("conditions"): [
                    voluptuous.All(str, voluptuous.Coerce(PullRequestRuleCondition))
                ],
                voluptuous.Required("actions"): actions.get_action_schemas(),
            }
        ],
        voluptuous.Length(min=1),
    )
)


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List

    def __post_init__(self):
        sorted_rules = sorted(self.rules, key=operator.itemgetter("name"))
        grouped_rules = itertools.groupby(sorted_rules, operator.itemgetter("name"))
        for name, sub_rules in grouped_rules:
            sub_rules = list(sub_rules)
            if len(sub_rules) == 1:
                continue
            for n, rule in enumerate(sub_rules):
                rule["name"] += " #%d" % (n + 1)

    def __iter__(self):
        return iter(self.rules)

    @classmethod
    def from_list(cls, lst):
        return cls(PullRequestRulesSchema(lst))

    def as_dict(self):
        return {
            "rules": [
                {
                    "name": rule["name"],
                    "hidden": rule["hidden"],
                    "conditions": list(map(str, rule["conditions"])),
                    "actions": dict(
                        (name, obj.config) for name, obj in rule["actions"].items()
                    ),
                }
                for rule in self.rules
            ]
        }

    @dataclasses.dataclass
    class PullRequestRuleForPR:
        """A pull request rule that matches a pull request."""

        # Fixed base attributes that are not considered when looking for the
        # next matching rules.
        BASE_ATTRIBUTES = (
            "head",
            "base",
            "author",
            "merged_by",
            "body",
            "title",
            "files",
        )
        TEAM_ATTRIBUTES = (
            "author",
            "merged_by",
            "approved-reviews-by",
            "dismissed-reviews-by",
            "commented-reviews-by",
        )

        # The list of pull request rules to match against.
        rules: typing.List

        # The context to test.
        context: context.Context

        # The rules matching the pull request.
        matching_rules: typing.List = dataclasses.field(
            init=False, default_factory=list
        )

        # The rules not matching the pull request.
        ignored_rules: typing.List = dataclasses.field(init=False, default_factory=list)

        def __post_init__(self):
            for rule in self.rules:
                ignore_rules = False
                next_conditions_to_validate = []
                for condition in rule["conditions"]:
                    for attrib in self.TEAM_ATTRIBUTES:
                        condition.set_value_expanders(
                            attrib, self.context.resolve_teams,
                        )

                    name = condition.get_attribute_name()
                    value = getattr(self.context.pull_request, name)
                    if not condition(**{name: value}):
                        next_conditions_to_validate.append(condition)
                        if condition.attribute_name in self.BASE_ATTRIBUTES:
                            ignore_rules = True

                if ignore_rules:
                    self.ignored_rules.append((rule, next_conditions_to_validate))
                else:
                    self.matching_rules.append((rule, next_conditions_to_validate))

    def get_pull_request_rule(self, pull_request):
        return self.PullRequestRuleForPR(self.rules, pull_request)


class YamlInvalid(voluptuous.Invalid):
    pass


class YamlInvalidPath(dict):
    def __init__(self, mark):
        super().__init__({"line": mark.line + 1, "column": mark.column + 1})

    def __repr__(self):
        return "at position {line}:{column}".format(**self)


class Yaml:
    def __init__(self, validator, **kwargs):
        self.validator = validator
        self._schema = voluptuous.Schema(validator, **kwargs)

    def __call__(self, v):
        try:
            v = yaml.safe_load(v)
        except yaml.YAMLError as e:
            error_message = str(e)
            path = None
            if hasattr(e, "problem_mark"):
                path = [YamlInvalidPath(e.problem_mark)]
                error_message += " (%s)" % path[0]
            raise YamlInvalid(message="Invalid yaml", error_message=str(e), path=path)

        return self._schema(v)

    def __repr__(self):
        return "Yaml(%s)" % repr(self.validator)


UserConfigurationSchema = voluptuous.Schema(
    Yaml(
        {
            voluptuous.Required("pull_request_rules"): voluptuous.Coerce(
                PullRequestRules.from_list
            )
        }
    )
)


class NoRules(Exception):
    def __init__(self):
        super().__init__("Mergify configuration file is missing")


class InvalidRules(Exception):
    def __init__(self, error):
        if isinstance(error, voluptuous.MultipleInvalid):
            message = "\n".join(map(str, error.errors))
        else:
            message = str(error)
        super().__init__(self, message)


MERGIFY_CONFIG_FILENAMES = (".mergify.yml", ".mergify/config.yml")


def get_mergify_config_content(ctxt, ref=None):
    """Get the Mergify configuration file content.

    :return: The filename and its content.
    """
    kwargs = {}
    if ref:
        kwargs["ref"] = ref
    for filename in MERGIFY_CONFIG_FILENAMES:
        try:
            content = ctxt.client.item(f"contents/{filename}", **kwargs)["content"]
        except httpx.HTTPNotFound:
            continue
        return filename, base64.b64decode(bytearray(content, "utf-8"))
    raise NoRules()


def get_mergify_config(ctxt, ref=None):
    filename, content = get_mergify_config_content(ctxt, ref)
    try:
        return filename, UserConfigurationSchema(content)
    except voluptuous.Invalid as e:
        raise InvalidRules(e)
