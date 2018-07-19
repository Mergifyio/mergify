# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
import logging
import re

import github
import voluptuous
import yaml

LOG = logging.getLogger(__name__)

with open("default_rule.yml", "r") as f:
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

# TODO(sileht): We can add some otherthing like
# automatic backport tag
# option to disable mergify on a particular PR
Rule = {
    'protection': Protection,
    'enabling_label': voluptuous.Any(None, str),
    'disabling_label': str,
    'disabling_files': [str],
    'merge_strategy': {
        "method": voluptuous.Any("rebase", "merge", "squash"),
        "rebase_fallback": voluptuous.Any("merge", "squash", "none"),
    },
    'automated_backport_labels': voluptuous.Any({str: str}, None),
}

UserConfigurationSchema = {
    voluptuous.Required('rules'): voluptuous.Any({
        'default': Rule,
        'branches': {str: voluptuous.Any(Rule, None)},
    }, None)
}


class NoRules(Exception):
    pass


class InvalidRules(Exception):
    pass


def validate_user_config(content):
    # NOTE(sileht): This is just to check the syntax some attributes can be
    # missing, the important thing is that once merged with the default.
    # Everything need by Github is set
    return voluptuous.Schema(UserConfigurationSchema)(yaml.safe_load(content))


def validate_merged_config(config):
    # NOTE(sileht): To be sure the POST request to protect branch works
    # we must have all keys set, so we set required=True here.
    # Optional key in Github API side have to be explicitly Optional with
    # voluptuous
    return voluptuous.Schema(Rule, required=True)(config)


def dict_merge(dct, merge_dct):
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict)
                and isinstance(merge_dct[k], collections.Mapping)):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]


def get_branch_rule(g_repo, branch, ref=github.GithubObject.NotSet):
    rule = copy.deepcopy(DEFAULT_RULE)

    try:
        content = g_repo.get_contents(".mergify.yml", ref=ref).decoded_content
        LOG.info("found mergify.yml")
    except github.GithubException as e:
        # NOTE(sileht): PyGithub is buggy here it should raise
        # UnknownObjectException. but depending of the error message
        # the convertion is not done and the generic exception is raise
        # so always catch the generic
        if e.status != 404:  # pragma: no cover
            raise
        raise NoRules(".mergify.yml is missing")

    try:
        rules = validate_user_config(content)["rules"] or {}
    except yaml.YAMLError as e:
        if hasattr(e, 'problem_mark'):
            raise InvalidRules(".mergify.yml is invalid at position: (%s:%s)" %
                               (e.problem_mark.line+1,
                                e.problem_mark.column+1))
        else:  # pragma: no cover
            raise InvalidRules(".mergify.yml is invalid: %s" % str(e))
    except voluptuous.MultipleInvalid as e:
        raise InvalidRules(".mergify.yml is invalid: %s" % str(e))

    dict_merge(rule, rules.get("default", {}))

    for branch_re in rules.get("branches", []):
        if re.match(branch_re, branch):
            if rules["branches"][branch_re] is None:
                LOG.info("Rule for %s branch: %s" % (branch, rule))
                return None
            else:
                dict_merge(rule, rules["branches"][branch_re])
    try:
        rule = validate_merged_config(rule)
    except voluptuous.MultipleInvalid as e:  # pragma: no cover
        raise InvalidRules("mergify configuration invalid: %s" % str(e))

    # NOTE(sileht): Always disable Mergify if its configuration is changed
    if ".mergify.yml" not in rule["disabling_files"]:
        rule["disabling_files"].append(".mergify.yml")

    LOG.info("Rule for %s branch: %s" % (branch, rule))
    return rule
