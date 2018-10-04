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

import github

from mergify_engine import config
from mergify_engine import utils


def create_jwt():
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    return integration.create_jwt()


def github_for(repo):
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    install_id = utils.get_installation_id(integration, repo.split("/")[0])
    installation_token = integration.get_access_token(install_id).token
    return github.Github(installation_token)


def get_pull(path):
    owner, repo, _, pull = path.split("/")
    g = github_for(owner + "/" + repo)
    return g.get_repo(owner + "/" + repo).get_pull(int(pull))


def get_combined_status(path):
    p = get_pull(path)
    commit = p.base.repo.get_commit(p.head.sha)
    return commit.get_combined_status()


def get_config(path):
    r = github_for(path).get_repo(path)
    return r.get_contents(".mergify.yml").decoded_content.decode()
