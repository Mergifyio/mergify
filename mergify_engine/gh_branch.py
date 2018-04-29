# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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
import sys

import github
import voluptuous
import yaml

from mergify_engine import config

LOG = logging.getLogger(__name__)

with open("default_rule.yml", "r") as f:
    DEFAULT_RULE = yaml.load(f.read())


def dict_merge(dct, merge_dct):
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict)
                and isinstance(merge_dct[k], collections.Mapping)):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]


def is_configured(g_repo, branch, rule):
    g_branch = g_repo.get_branch(branch)

    if not g_branch.protected:
        return rule is None
    elif rule is None:
        return False

    headers, data = g_repo._requester.requestJsonAndCheck(
        "GET", g_repo.url + "/branches/" + branch + '/protection',
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )

    # NOTE(sileht): Transform the payload into rule
    del data['url']
    del data["required_status_checks"]["url"]
    del data["required_status_checks"]["contexts_url"]
    del data["required_pull_request_reviews"]["url"]
    del data["enforce_admins"]["url"]
    data["required_status_checks"]["contexts"] = sorted(
        data["required_status_checks"]["contexts"])
    data["enforce_admins"] = data["enforce_admins"]["enabled"]

    if "restrictions" not in data:
        data["restrictions"] = None

    rule['protection']["required_status_checks"]["contexts"] = sorted(
        rule['protection']["required_status_checks"]["contexts"])

    return rule['protection'] == data


def protect(g_repo, branch, rule):
    if g_repo.organization:
        rule['protection']['required_pull_request_reviews'][
            'dismissal_restrictions'] = {}

    # NOTE(sileht): Not yet part of the API
    # maybe soon https://github.com/PyGithub/PyGithub/pull/527
    g_repo._requester.requestJsonAndCheck(
        'PUT',
        "{base_url}/branches/{branch}/protection".format(base_url=g_repo.url,
                                                         branch=branch),
        input=rule['protection'],
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )


def unprotect(g_repo, branch):
    # NOTE(sileht): Not yet part of the API
    # maybe soon https://github.com/PyGithub/PyGithub/pull/527
    g_repo._requester.requestJsonAndCheck(
        'DELETE',
        "{base_url}/branches/{branch}/protection".format(base_url=g_repo.url,
                                                         branch=branch),
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )

# TODO(sileht): move rule parsing on another module
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
})

UserConfigurationSchema = voluptuous.Schema({
    'rules': voluptuous.Any({
        voluptuous.Optional('default'): Rule,
        # TODO(sileht): allow None to disable mergify on a specific branch
        voluptuous.Optional('branches'): {str: Rule},
    }, None)
}, required=True)


def validate_rule(content):
    return UserConfigurationSchema(yaml.load(content))


class NoRules(Exception):
    pass


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


def configure_protection_if_needed(g_repo, branch, rule):
    if not is_configured(g_repo, branch, rule):
        LOG.warning("Branch %s of %s is misconfigured, configuring it to %s",
                    branch, g_repo.full_name, rule)
        if rule is None:
            unprotect(g_repo, branch)
        else:
            protect(g_repo, branch, rule)


def test():
    import github

    from mergify_engine import gh_pr
    from mergify_engine import utils

    utils.setup_logging()
    config.log()
    gh_pr.monkeypatch_github()

    parts = sys.argv[1].split("/")

    LOG.info("Getting repo %s ..." % sys.argv[1])

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    installation_id = utils.get_installation_id(integration, parts[3])
    token = integration.get_access_token(installation_id).token
    g = github.Github(token)
    user = g.get_user(parts[3])
    repo = user.get_repo(parts[4])
    LOG.info("Protecting repo %s branch %s ..." % (sys.argv[1], sys.argv[2]))
    rule = get_branch_rule(repo, sys.argv[2])
    configure_protection_if_needed(repo, sys.argv[2], rule)


if __name__ == '__main__':
    test()
