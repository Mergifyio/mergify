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

import logging
import sys

from mergify_engine import config
from mergify_engine import rules

LOG = logging.getLogger(__name__)


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
    rule = rules.get_branch_rule(repo, sys.argv[2])
    configure_protection_if_needed(repo, sys.argv[2], rule)


if __name__ == '__main__':
    test()
