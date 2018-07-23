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

import copy
import logging

import github

LOG = logging.getLogger(__name__)


def is_configured(g_repo, branch, rule, data):

    if data is None:
        return rule is None
    elif rule is None:
        return False

    # NOTE(sileht): Transform the payload into rule
    del data['url']

    if "required_status_checks" in data:
        del data["required_status_checks"]["url"]
        del data["required_status_checks"]["contexts_url"]
        data["required_status_checks"]["contexts"] = sorted(
            data["required_status_checks"]["contexts"])
    else:
        data["required_status_checks"] = None

    if "required_pull_request_reviews" in data:
        del data["required_pull_request_reviews"]["url"]
    else:
        data["required_pull_request_reviews"] = None

    del data["enforce_admins"]["url"]
    data["enforce_admins"] = data["enforce_admins"]["enabled"]

    if "restrictions" not in data:
        data["restrictions"] = None

    rsc = rule["protection"].get("required_status_checks")
    if rsc and "contexts" in rsc:
        rsc["contexts"] = sorted(rsc["contexts"])

    configured = rule['protection'] == data
    if not configured:
        LOG.warning("Branch %s of %s is misconfigured: %s != %s",
                    branch, g_repo.full_name, data, rule['protection'])
    return configured


def protect(g_repo, branch, rule):
    if g_repo.organization and rule['protection'][
            'required_pull_request_reviews']:
        rule = copy.deepcopy(rule)
        rule['protection']['required_pull_request_reviews'][
            'dismissal_restrictions'] = {}

    # NOTE(sileht): Not yet part of the API
    # maybe soon https://github.com/PyGithub/PyGithub/pull/527
    return g_repo._requester.requestJsonAndCheck(
        'PUT',
        "{base_url}/branches/{branch}/protection".format(
            base_url=g_repo.url, branch=branch),
        input=rule['protection'],
        headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
    )


def unprotect(g_repo, branch):
    # NOTE(sileht): Not yet part of the API
    # maybe soon https://github.com/PyGithub/PyGithub/pull/527
    try:
        g_repo._requester.requestJsonAndCheck(
            'DELETE',
            "{base_url}/branches/{branch}/protection".format(
                base_url=g_repo.url, branch=branch),
            headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
        )
    except github.GithubException as e:  # pragma: no cover
        if e.status == 404 and e.data["message"] == 'Branch not protected':
            return
        raise


def get_protection(g_repo, branch):
    try:
        headers, data = g_repo._requester.requestJsonAndCheck(
            "GET", g_repo.url + "/branches/" + branch + '/protection',
            headers={'Accept': 'application/vnd.github.luke-cage-preview+json'}
        )
        return data
    except github.GithubException as e:
        if e.status == 404 and e.data["message"] == "Branch not protected":
            return None
        else:
            raise


def configure_protection_if_needed(g_repo, branch, rule):

    data = get_protection(g_repo, branch)
    if not is_configured(g_repo, branch, rule, data):
        # NOTE(sileht): Updating some value are a bit broken, like setting
        # null to disable an already set required_pull_request_reviews or
        # required_status_checks. So to be sure we setup what we want
        # remove everything and then protect the branch
        unprotect(g_repo, branch)
        if rule is not None:
            headers, data = protect(g_repo, branch, rule)

        if not is_configured(g_repo, branch, rule, data):
            raise RuntimeError("Branch %s of %s is still misconfigured, "
                               "abandoning" % (branch, g_repo.full_name))
