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

import pprint

import github

from mergify_engine import branch_protection
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import utils


def create_jwt():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    return integration.create_jwt()


def report(url):
    path = url.replace("https://github.com/", "")
    owner, repo, _, pull_number = path.split("/")

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    install_id = utils.get_installation_id(integration, owner)
    installation_token = integration.get_access_token(install_id).token

    g = github.Github(installation_token,
                      base_url="https://api.%s" % config.GITHUB_DOMAIN)
    r = g.get_repo(owner + "/" + repo)
    p = r.get_pull(int(pull_number))

    print("* CONFIGURATION:")
    print(r.get_contents(".mergify.yml").decoded_content.decode())

    print("* BRANCH PROTECTION:")
    pprint.pprint(branch_protection.get_protection(r, p.base.ref), width=160)

    mp = mergify_pull.MergifyPull(p, installation_token)
    print("* PULL REQUEST:")
    pprint.pprint(mp.to_dict(), width=160)
    print("is_behind: %s" % mp.is_behind())
    print("mergeable_state: %s" % mp.g_pull.mergeable_state)

    print("* MERGIFY STATUSES:")
    commit = p.base.repo.get_commit(p.head.sha)
    for s in commit.get_combined_status().statuses:
        if s.context.startswith("mergify"):
            print("[%s]: %s" % (s.context, s.state))

    print("* MERGIFY CHECKS:")
    checks = list(check_api.get_checks(p))
    for c in checks:
        if c.name.startswith("Mergify"):
            print("[%s]: %s | %s" % (c.name, c.conclusion,
                                     c.output.get("title")))
            print("> " + "\n> ".join(c.output.get("summary").split("\n")))
    return g, p


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Debugger for mergify'
    )
    parser.add_argument("url", help="Pull request url")
    args = parser.parse_args()
    report(args.url)
