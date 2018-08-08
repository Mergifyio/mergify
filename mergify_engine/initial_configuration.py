# -*- encoding: utf-8 -*-
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

import daiquiri

import github

import yaml

from mergify_engine import config

LOG = daiquiri.getLogger(__name__)

INITIAL_CONFIG_BRANCH = "mergify/initial-config"


def sanitize_context(context):
    # NOTE(sileht): Some CI report different for pr and branch
    # We can't require branch testing on PR, so remove the detail
    # * travis: remove /pr, /push
    if context.endswith("/pr"):
        context = context[:-3]
    elif context.endswith("/push"):
        context = context[:-5]
    return context


def create_pull_request(installation_token, repo):
    pulls = list(repo.get_pulls(state="all"))[:config.INITIAL_PULLS_LOOKUP]
    contexts = set()
    for pull in pulls:
        contexts.update(
            sanitize_context(s.context)
            for s in repo.get_commit(pull.head.sha).get_statuses()
            if not s.context.startswith("mergify/")
        )

    mergify_config = {
        "rules": {
            "default": {
                "protection": {
                    "required_status_checks": {
                        "contexts": list(contexts),
                    },
                    "required_pull_request_reviews": {
                        "required_approving_review_count": 1
                    },
                },
            },
        }
    }

    content = yaml.dump(mergify_config, default_flow_style=False)

    try:
        default_branch = repo.get_branch(repo.default_branch)
    except github.GithubException as e:
        if e.status != 404:
            raise
        # TODO(sileht): When an empty repo is created we can't get the default
        # branch this one doesn't yet exists. We may want to pospone the first
        # PR in this case. For now just return to not raise backtrace
        return

    try:
        parents = [repo.get_git_commit(default_branch.commit.sha)]
    except github.GithubException as e:
        if e.status == 409 and e.data['message'] == 'Git Repository is empty.':
            return
        raise
    message = "Mergify initial configuration"
    tree = repo.create_git_tree([
        github.InputGitTreeElement(".mergify.yml", "100644", "blob", content)
    ], base_tree=default_branch.commit.commit.tree)
    commit = repo.create_git_commit(message, tree, parents)
    repo.create_git_ref("refs/heads/%s" % INITIAL_CONFIG_BRANCH, commit.sha)
    repo.create_pull(
        title=message,
        body="""This is an initial configuration for Mergify.

This pull request will require one reviewer approval.

Those required [status checks](https://doc.mergify.io/configuration.html#required-status-checks) have been discovered from the %d more recent pull requests of your project:

%s

More information about Mergify configuration can be found at https://doc.mergify.io

To modify this pull request, you can check it out locally.
See documentation: https://help.github.com/articles/checking-out-pull-requests-locally/
""" % (config.INITIAL_PULLS_LOOKUP, "\n *".join(contexts)),  # noqa
        base=repo.default_branch,
        head=INITIAL_CONFIG_BRANCH,
    )
    LOG.info('Initial configuration created', repository=repo.full_name)


def create_pull_request_if_needed(installation_token, repo):
    try:
        repo.get_file_contents(".mergify.yml")
    except github.GithubException as e:
        if e.status != 404:  # pragma: no cover
            raise
        try:
            repo.get_branch(INITIAL_CONFIG_BRANCH)
        except github.GithubException as e:
            if e.status != 404:  # pragma: no cover
                raise
            create_pull_request(installation_token, repo)
        else:
            LOG.info("Initial configuration branch already exists",
                     repository=repo.full_name)
    else:
        LOG.info("Mergify already configured", repository=repo.full_name)
