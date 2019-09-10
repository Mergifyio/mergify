# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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

import itertools
import operator

import attr

import daiquiri

import github

import pkg_resources

import voluptuous

import yaml

from mergify_engine.rules import filter


LOG = daiquiri.getLogger(__name__)


def PullRequestRuleCondition(value):
    try:
        return filter.Filter.parse(value)
    except filter.parser.pyparsing.ParseException as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)),
            error_message=str(e))
    except filter.InvalidQuery as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)),
            error_message=str(e))


PullRequestRulesSchema = voluptuous.Schema(voluptuous.All([{
    voluptuous.Required('name'): str,
    voluptuous.Required('hidden', default=False): bool,
    voluptuous.Required('conditions'): [
        voluptuous.All(str, voluptuous.Coerce(
            PullRequestRuleCondition))
    ],
    voluptuous.Required("actions"): dict(
        (ep.name, voluptuous.All(
            ep.load().validator,
            voluptuous.Coerce(ep.load())
        )) for ep in pkg_resources.iter_entry_points("mergify_actions"))
}], voluptuous.Length(min=1)))


def load_pull_request_rules_schema(rules):
    rules = PullRequestRulesSchema(rules)

    sorted_rules = sorted(rules, key=operator.itemgetter('name'))
    grouped_rules = itertools.groupby(sorted_rules,
                                      operator.itemgetter('name'))
    for name, sub_rules in grouped_rules:
        sub_rules = list(sub_rules)
        if len(sub_rules) == 1:
            continue
        for n, rule in enumerate(sub_rules):
            rule["name"] += " #%d" % (n + 1)

    return rules


@attr.s
class PullRequestRules:
    rules = attr.ib(converter=load_pull_request_rules_schema)

    def as_dict(self):
        return {'rules': [{
            "name": rule["name"],
            "hidden": rule["hidden"],
            "conditions": list(map(str, rule["conditions"])),
            "actions": dict(
                (name, obj.config)
                for name, obj in rule["actions"].items()
            ),
        } for rule in self.rules]}

    @attr.s
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
            "review-requested",
            "approved-reviews-by",
            "dismissed-reviews-by",
            "commented-reviews-by",
        )

        # The list of pull request rules to match against.
        rules = attr.ib()
        # The pull request to test.
        pull_request = attr.ib()

        # The rules matching the pull request.
        matching_rules = attr.ib(init=False, default=attr.Factory(list))

        # The rules not matching the pull request.
        ignored_rules = attr.ib(init=False, default=attr.Factory(list))

        def __attrs_post_init__(self):
            d = self.pull_request.to_dict()
            for rule in self.rules:
                ignore_rules = False
                next_conditions_to_validate = []
                for condition in rule['conditions']:
                    for attrib in self.TEAM_ATTRIBUTES:
                        condition.set_value_expanders(
                            attrib, self.pull_request.resolve_teams)
                    if not condition(**d):
                        next_conditions_to_validate.append(condition)
                        if condition.attribute_name in self.BASE_ATTRIBUTES:
                            ignore_rules = True

                if ignore_rules:
                    self.ignored_rules.append(
                        (rule, next_conditions_to_validate))
                else:
                    self.matching_rules.append(
                        (rule, next_conditions_to_validate))

    def get_pull_request_rule(self, pull_request):
        return self.PullRequestRuleForPR(self.rules, pull_request)


class YamlInvalid(voluptuous.Invalid):
    pass


class YamlInvalidPath(dict):
    def __init__(self, mark):
        super().__init__({
            "line": mark.line + 1,
            "column": mark.column + 1,
        })

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
            if hasattr(e, 'problem_mark'):
                path = [YamlInvalidPath(e.problem_mark)]
                error_message += " (%s)" % path[0]
            raise YamlInvalid(
                message="Invalid yaml",
                error_message=str(e),
                path=path
            )

        return self._schema(v)

    def __repr__(self):
        return 'Yaml(%s)' % repr(self.validator)


UserConfigurationSchema = voluptuous.Schema(Yaml({
    voluptuous.Required("pull_request_rules"):
    voluptuous.Coerce(PullRequestRules),
}))


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


def get_mergify_config_content(repository, ref=github.GithubObject.NotSet):
    for filename in MERGIFY_CONFIG_FILENAMES:
        try:
            return repository.get_contents(filename, ref=ref).decoded_content
        except github.GithubException as e:  # pragma: no cover
            # NOTE(sileht): PyGithub is buggy here it should raise
            # UnknownObjectException. but depending of the error message
            # the convertion is not done and the generic exception is raise
            # so always catch the generic
            if e.status != 404:
                raise
    raise NoRules()


def get_mergify_config(repository, ref=github.GithubObject.NotSet):
    try:
        return UserConfigurationSchema(
            get_mergify_config_content(repository, ref)
        )
    except voluptuous.Invalid as e:
        raise InvalidRules(e)
