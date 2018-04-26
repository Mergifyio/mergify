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
import os
import re
import sys

import github
import voluptuous
import yaml

from mergify_engine import config

LOG = logging.getLogger(__name__)

with open("default_policy.yml", "r") as f:
    DEFAULT_POLICY = yaml.load(f.read())


def dict_merge(dct, merge_dct):
    for k, v in merge_dct.items():
        if (k in dct and isinstance(dct[k], dict)
                and isinstance(merge_dct[k], collections.Mapping)):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]


def is_protected(g_repo, branch, policy):
    g_branch = g_repo.get_branch(branch)
    if not g_branch.protected:
        return False

    headers, data = g_repo._requester.requestJsonAndCheck(
        "GET", g_repo.url + "/branches/" + branch + '/protection',
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )

    # NOTE(sileht): Transform the payload into policy
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

    policy["required_status_checks"]["contexts"] = sorted(
        policy["required_status_checks"]["contexts"])

    return policy == data


def protect(g_repo, branch, policy):
    if g_repo.organization:
        policy['required_pull_request_reviews']['dismissal_restrictions'] = {}

    # NOTE(sileht): Not yet part of the API
    # maybe soon https://github.com/PyGithub/PyGithub/pull/527
    g_repo._requester.requestJsonAndCheck(
        'PUT',
        "{base_url}/branches/{branch}/protection".format(base_url=g_repo.url,
                                                         branch=branch),
        input=policy,
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )


Policy = voluptuous.Schema({
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

UserConfigurationSchema = voluptuous.Schema({
    'policies': {
        voluptuous.Optional('default'): Policy,
        # TODO(sileht): allow None to disable mergify on a specific branch
        voluptuous.Optional('branches'): {str: Policy},
    }
}, required=True)


def validate_policy(content):
    return UserConfigurationSchema(yaml.load(content))


class NoPolicies(Exception):
    pass


def get_branch_policy(g_repo, branch):
    # TODO(sileht): Ensure the file is valid
    policy = copy.deepcopy(DEFAULT_POLICY)

    try:
        content = g_repo.get_contents(".mergify.yml").decoded_content
        LOG.info("found mergify.yml")
    except github.UnknownObjectException:
        # NOTE(sileht): Fallback to a local file
        f = "%s_policy.yml" % g_repo.owner.login
        if os.path.exists(f):
            LOG.info("fallback to local %s", f)
            with open(f, "r") as f:
                content = f.read()
        else:
            raise NoPolicies(".mergify.yml is missing")

    try:
        policies = validate_policy(content)["policies"]
    except voluptuous.MultipleInvalid as e:
        raise NoPolicies("Content of .mergify.yml is invalid: %s" % str(e))

    dict_merge(policy, policies.get("default", {}))

    for branch_re in policies.get("branches", []):
        if re.match(branch_re, branch):
            dict_merge(policy, policies["branches"][branch_re])

    LOG.info("Policy for %s branch: %s" % (branch, policy))
    return policy


def protect_if_needed(g_repo, branch, policy):
    if not is_protected(g_repo, branch, policy):
        LOG.warning("Branch %s of %s is misconfigured, configuring it to %s",
                    branch, g_repo.full_name, policy)
        protect(g_repo, branch, policy)


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
    policy = get_branch_policy(repo, sys.argv[2])
    protect_if_needed(repo, sys.argv[2], policy)


if __name__ == '__main__':
    test()
