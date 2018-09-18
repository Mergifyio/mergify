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

import collections
import copy
import re

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


Protection = {
    'required_status_checks': voluptuous.Any(
        None, {
            voluptuous.Required('strict', default=False): bool,
            voluptuous.Required('contexts', default=[]): [str],
        }),
    'required_pull_request_reviews': voluptuous.Any(
        None, {
            'dismiss_stale_reviews': bool,
            'require_code_owner_reviews': bool,
            'required_approving_review_count': voluptuous.All(
                int, voluptuous.Range(min=1, max=6)),
        }),
    'restrictions': voluptuous.Any(None, {
        voluptuous.Required('teams', default=[]): [str],
        voluptuous.Required('users', default=[]): [str],
    }),
    'enforce_admins': voluptuous.Any(None, bool),
}

MergeMethods = voluptuous.Any("rebase", "merge", "squash")
RebaseFallback = voluptuous.Any("merge", "squash", "none")

# TODO(sileht): We can add some otherthing like
# automatic backport tag
# option to disable mergify on a particular PR
Rule = {
    'protection': Protection,
    'enabling_label': voluptuous.Any(None, str),
    'disabling_label': str,
    'disabling_files': [str],
    'merge_strategy': {
        "method": MergeMethods,
        "rebase_fallback": RebaseFallback,
    },
    'automated_backport_labels': voluptuous.Any({str: str}, None),
}


def PullRequestRuleCondition(value):
    if not isinstance(value, str):
        raise voluptuous.Invalid("Condition must be a string")
    try:
        return filter.Filter.parse(value)
    except filter.parser.pyparsing.ParseException as e:
        raise voluptuous.Invalid(
            message="Invalid condition '%s'. %s" % (value, str(e)),
            error_message=str(e))


PullRequestRuleConditionsSchema = voluptuous.Schema([
    voluptuous.Any(
        PullRequestRuleCondition,
        voluptuous.All(
            {
                voluptuous.Any("and", "or"): voluptuous.Self,
            },
            voluptuous.Length(min=1, max=1),
        ),
    ),
])


PullRequestRulesSchema = voluptuous.Schema([{
    voluptuous.Required('name'): str,
    voluptuous.Required('conditions'): PullRequestRuleConditionsSchema,
    voluptuous.Required("merge", default=False): voluptuous.Any(
        False,
        {
            voluptuous.Required("method",
                                default="merge"): MergeMethods,
            voluptuous.Required("rebase_fallback",
                                default="merge"): RebaseFallback,
            voluptuous.Required("strict", default=False): bool,
        },
    ),
    voluptuous.Required("backport", default=False):
    voluptuous.Any(False, [str]),
}])


@attr.s
class PullRequestRules:
    rules = attr.ib(converter=PullRequestRulesSchema)

    @attr.s
    class PullRequestRuleForPR:
        """A pull request rule that matches a pull request."""

        # The list of pull request rules to match against.
        rules = attr.ib()
        # The pull request to test.
        pull_request = attr.ib()

        # The rules matching the pull request.
        matching_rules = attr.ib(init=False, default=attr.Factory(list))
        # The rules that could match in some conditions is coming.
        next_rules = attr.ib(init=False, default=attr.Factory(list))
        # The final rule where all matching rules are merged
        rule = attr.ib(init=False, default=attr.Factory(dict))

        def __attrs_post_init__(self):
            d = self.pull_request.to_dict()
            for rule in self.rules:
                for condition in rule['conditions']:
                    if not condition(**d):
                        self.next_rules.append((rule, condition))
                        break
                else:
                    self.matching_rules.append(rule)
                    self.rule.update(rule)
            try:
                del self.rule['name']
                del self.rule['conditions']
            except KeyError:
                # Can happen if no match
                pass

        def next_conditions_for(self, feature):
            """Conditions that must match for the feature to be enabled."""
            return list(rule[1]
                        for rule in self.next_rules
                        if feature in rule)

    def get_pull_request_rule(self, pull_request):
        return self.PullRequestRuleForPR(self.rules, pull_request)


OldUserConfigurationSchema = {
    voluptuous.Required('rules'): voluptuous.Any({
        'default': voluptuous.Any(Rule, None),
        'branches': {str: voluptuous.Any(Rule, None)},
    }, None)
}


NewUserConfigurationSchema = voluptuous.Schema({
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
    return voluptuous.Schema(OldUserConfigurationSchema)(
        yaml.safe_load(content))


def validate_merged_config(config):
    # NOTE(sileht): To be sure the POST request to protect branch works
    # we must have all keys set, so we set required=True here.
    # Optional key in Github API side have to be explicitly Optional with
    # voluptuous
    return voluptuous.Schema(Rule, required=True)(config)


def _dict_merge(dct, merge_dct):
    """Recursively merge keys/values from merge_dct into dct.

    :return: dct
    """
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict) and
           isinstance(merge_dct[k], collections.Mapping)):
            _dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]
    return dct


def get_merged_branch_rule(rules, branch_re=None):
    default_rules = copy.deepcopy(DEFAULT_RULE)
    if rules.get("default") is not None:
        default_rules = _dict_merge(default_rules, rules["default"])
    if branch_re:
        if rules["branches"][branch_re] is None:
            return None
        return _dict_merge(default_rules, rules["branches"][branch_re])
    elif "default" in rules and rules["default"] is None:
        return None
    return default_rules


def build_branch_rule(rules, branch):
    for branch_re in sorted(rules.get("branches", {})):
        if ((branch_re[0] == "^" and re.match(branch_re, branch)) or
           (branch_re[0] != "^" and branch_re == branch)):
            if rules["branches"][branch_re] is None:
                return None
            return get_merged_branch_rule(rules, branch_re)

    return get_merged_branch_rule(rules)


def get_mergify_config(repository, ref=github.GithubObject.NotSet):
    try:
        content = repository.get_contents(
            ".mergify.yml", ref=ref).decoded_content
    except github.GithubException as e:
        # NOTE(sileht): PyGithub is buggy here it should raise
        # UnknownObjectException. but depending of the error message
        # the convertion is not done and the generic exception is raise
        # so always catch the generic
        if e.status != 404:  # pragma: no cover
            raise
        raise NoRules()
    try:
        return validate_user_config(content) or {}
    except yaml.YAMLError as e:
        if hasattr(e, 'problem_mark'):
            raise InvalidRules("position (%s:%s)" %
                               (e.problem_mark.line + 1,
                                e.problem_mark.column + 1))
        else:  # pragma: no cover
            raise InvalidRules(str(e))
    except voluptuous.MultipleInvalid as e:
        raise InvalidRules(str(e))


def get_branch_rule(rules, branch):
    if rules is None:
        return None

    rule = build_branch_rule(rules, branch)
    if rule is None:
        return None

    try:
        rule = validate_merged_config(rule)
    except voluptuous.MultipleInvalid as e:  # pragma: no cover
        raise InvalidRules(str(e))

    # NOTE(sileht): Always disable Mergify if its configuration is changed
    if ".mergify.yml" not in rule["disabling_files"]:
        rule["disabling_files"].append(".mergify.yml")

    LOG.info("Fetched branch rule", branch=branch, rule=rule)
    return rule
