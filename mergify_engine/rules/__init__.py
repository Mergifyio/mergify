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

import attr

import daiquiri

import github

import pkg_resources

import voluptuous

import yaml

from mergify_engine.rules import filter


LOG = daiquiri.getLogger(__name__)

default_rule = pkg_resources.resource_filename(__name__,
                                               "../data/default_rule.yml")
with open(default_rule, "r") as f:
    DEFAULT_RULE = yaml.safe_load(f.read())


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


PullRequestRulesSchema = voluptuous.Schema([{
    voluptuous.Required('name'): str,
    voluptuous.Required('conditions'): [
        voluptuous.All(str, voluptuous.Coerce(
            PullRequestRuleCondition))
    ],
    voluptuous.Required("actions"): dict(
        (ep.name, voluptuous.All(
            ep.load().validator,
            voluptuous.Coerce(ep.load())
        )) for ep in pkg_resources.iter_entry_points("mergify_actions"))
}])


@attr.s
class PullRequestRules:
    rules = attr.ib(converter=PullRequestRulesSchema)

    def as_dict(self):
        return {'rules': [{
            "name": rule["name"],
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

        def __attrs_post_init__(self):
            d = self.pull_request.to_dict()
            for rule in self.rules:
                next_conditions_to_validate = []
                for condition in rule['conditions']:
                    for attrib in self.TEAM_ATTRIBUTES:
                        condition.set_value_expanders(
                            attrib, self.pull_request.resolve_teams)
                    if not condition(**d):
                        if condition.attribute_name in self.BASE_ATTRIBUTES:
                            # Ignore this rule
                            break
                        else:
                            next_conditions_to_validate.append(condition)
                else:
                    self.matching_rules.append(
                        (rule, next_conditions_to_validate))

    def get_pull_request_rule(self, pull_request):
        return self.PullRequestRuleForPR(self.rules, pull_request)


UserConfigurationSchemaV2 = voluptuous.Schema({
    voluptuous.Required("pull_request_rules"):
    voluptuous.Coerce(PullRequestRules),
})


class NoRules(Exception):
    def __init__(self):
        super().__init__(".mergify.yml is missing")


class InvalidRules(Exception):
    def __init__(self, detail):
        super().__init__("Mergify configuration is invalid: %s" % detail)


def validate_user_config(content):
    # NOTE(sileht): This is just to check the syntax some attributes can be
    # missing, the important thing is that once merged with the default.
    # Everything need by Github is set
    try:
        return voluptuous.Schema(UserConfigurationSchemaV2)(
            yaml.safe_load(content))
    except yaml.YAMLError as e:
        if hasattr(e, 'problem_mark'):
            raise InvalidRules("position (%s:%s)" %
                               (e.problem_mark.line + 1,
                                e.problem_mark.column + 1))
        else:  # pragma: no cover
            raise InvalidRules(str(e))
    except voluptuous.MultipleInvalid as e:
        raise InvalidRules(str(e))


def get_mergify_config(repository, ref=github.GithubObject.NotSet):
    try:
        content = repository.get_contents(
            ".mergify.yml", ref=ref).decoded_content
    except github.GithubException as e:  # pragma: no cover
        # NOTE(sileht): PyGithub is buggy here it should raise
        # UnknownObjectException. but depending of the error message
        # the convertion is not done and the generic exception is raise
        # so always catch the generic
        if e.status != 404:
            raise
        raise NoRules()

    return validate_user_config(content) or {}
