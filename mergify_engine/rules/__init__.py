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
import functools
import itertools
import operator
import typing

import voluptuous
import yaml

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.clients import http
from mergify_engine.rules import filter
from mergify_engine.rules import types


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


@dataclasses.dataclass
class Rule:
    name: str
    conditions: typing.List[filter.Filter]
    actions: typing.Dict[str, actions.Action]
    hidden: bool = False

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclasses.dataclass
class PullRequestRules:
    rules: typing.List[Rule]

    def __post_init__(self):
        # Make sure each rule has a unique name
        sorted_rules = sorted(self.rules, key=operator.attrgetter("name"))
        grouped_rules = itertools.groupby(sorted_rules, operator.attrgetter("name"))
        for name, sub_rules in grouped_rules:
            sub_rules = list(sub_rules)
            if len(sub_rules) == 1:
                continue
            for n, rule in enumerate(sub_rules):
                rule.name += " #%d" % (n + 1)

    def __iter__(self):
        return iter(self.rules)

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
                for condition in rule.conditions:
                    for attrib in self.TEAM_ATTRIBUTES:
                        condition.set_value_expanders(
                            attrib,
                            self.context.resolve_teams,
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


class YAMLInvalid(voluptuous.Invalid):
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


def YAML(v):
    try:
        return yaml.safe_load(v)
    except yaml.YAMLError as e:
        error_message = str(e)
        path = (
            [types.LineColumnPath(e.problem_mark.line + 1, e.problem_mark.column + 1)]
            if hasattr(e, "problem_mark")
            else None
        )
        raise YAMLInvalid(
            message="Invalid YAML", error_message=error_message, path=path
        )
    return v


PullRequestRulesSchema = voluptuous.All(
    [
        voluptuous.All(
            {
                voluptuous.Required("name"): str,
                voluptuous.Required("hidden", default=False): bool,
                voluptuous.Required("conditions"): [
                    voluptuous.All(str, voluptuous.Coerce(PullRequestRuleCondition))
                ],
                voluptuous.Required("actions"): actions.get_action_schemas(),
            },
            voluptuous.Coerce(Rule.from_dict),
        ),
    ],
    voluptuous.Length(min=1),
    voluptuous.Coerce(PullRequestRules),
)


UserConfigurationSchema = voluptuous.Schema(
    voluptuous.And(
        voluptuous.Coerce(YAML),
        {voluptuous.Required("pull_request_rules"): PullRequestRulesSchema},
    )
)


class NoRules(Exception):
    def __init__(self):
        super().__init__("Mergify configuration file is missing")


@dataclasses.dataclass
class InvalidRules(Exception):
    error: voluptuous.Invalid
    filename: str

    @staticmethod
    def _format_error(error):
        msg = str(error)
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


MERGIFY_CONFIG_FILENAMES = (
    ".mergify.yml",
    ".mergify/config.yml",
    ".github/mergify.yml",
)


def get_mergify_config_content(client, repo, ref=None):
    """Get the Mergify configuration file content.

    :return: The filename and its content.
    """
    kwargs = {}
    if ref:
        kwargs["ref"] = ref
    for filename in MERGIFY_CONFIG_FILENAMES:
        try:
            content = client.item(
                f"/repos/{client.auth.owner}/{repo}/contents/{filename}", **kwargs
            )["content"]
        except http.HTTPNotFound:
            continue
        return filename, base64.b64decode(bytearray(content, "utf-8"))
    raise NoRules()


def get_mergify_config(client, repo, ref=None):
    filename, content = get_mergify_config_content(client, repo, ref)
    try:
        return filename, UserConfigurationSchema(content)
    except voluptuous.Invalid as e:
        raise InvalidRules(e, filename)
