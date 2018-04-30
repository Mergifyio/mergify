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
    DEFAULT_RULE = yaml.load(f.read())


Protection = voluptuous.Schema({
    'required_status_checks': voluptuous.Any(
        None, {
            'strict': bool,
            'contexts': [str],
        }),
    'required_pull_request_reviews': {
        'dismiss_stale_reviews': bool,
        'require_code_owner_reviews': bool,
        'required_approving_review_count': int,
    },
    'restrictions': voluptuous.Any(None, []),
    'enforce_admins': bool,
})

# TODO(sileht): We can add some otherthing like
# automatic backport tag
# option to disable mergify on a particular PR
Rule = voluptuous.Schema({
    'protection': Protection,
    'disabling_label': str,
    'automated_backport_labels': {str: str},
})

UserConfigurationSchema = voluptuous.Schema({
    voluptuous.Required('rules'): voluptuous.Any({
        'default': Rule,
        'branches': {str: voluptuous.Any(Rule, None)},
    }, None)
})


class NoRules(Exception):
    pass


def validate_rule(content):
    return UserConfigurationSchema(yaml.load(content))


def dict_merge(dct, merge_dct):
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict)
                and isinstance(merge_dct[k], collections.Mapping)):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]


def get_branch_rule(g_repo, branch):
    # TODO(sileht): Ensure the file is valid
    rule = copy.deepcopy(DEFAULT_RULE)

    try:
        content = g_repo.get_contents(".mergify.yml").decoded_content
        LOG.info("found mergify.yml")
    except github.UnknownObjectException:
        raise NoRules(".mergify.yml is missing")

    try:
        rules = validate_rule(content)["rules"] or {}
    except voluptuous.MultipleInvalid as e:
        raise NoRules(".mergify.yml is invalid: %s" % str(e))

    dict_merge(rule, rules.get("default", {}))

    for branch_re in rules.get("branches", []):
        if re.match(branch_re, branch):
            if rules["branches"][branch_re] is None:
                LOG.info("Rule for %s branch: %s" % (branch, rule))
                return None
            else:
                dict_merge(rule, rules["branches"][branch_re])

    LOG.info("Rule for %s branch: %s" % (branch, rule))
    return rule
