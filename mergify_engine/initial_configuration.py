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

import logging
import yaml

import github

from mergify_engine import config

LOG = logging.getLogger(__name__)

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
            "protection": {
                "required_status_checks": {
                    "contexts": list(contexts),
                },
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1
                },
            },
        }
    }

    content = yaml.dump(mergify_config, default_flow_style=False)

    default_branch = repo.get_branch(repo.default_branch)

    message = "Mergify initial configuration"
    parents = [repo.get_git_commit(default_branch.commit.sha)]
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
    LOG.info('Initial configuration created for repo %s', repo.full_name)


def create_pull_request_if_needed(installation_token, repo):
    try:
        repo.get_file_contents(".mergify.yml")
    except github.UnknownObjectException:
        try:
            repo.get_branch(INITIAL_CONFIG_BRANCH)
        except github.GithubException as e:
            # NOTE(sileht): PyGitHub is buggy here it should
            # UnknownObjectException. but because the message is "Branch not
            # found", instead of "Not found", we got the generic exception.
            if e.status != 404:  # pragma: no cover
                raise
            create_pull_request(installation_token, repo)
        else:
            LOG.info("Initial configuration branch already exists for repo %s",
                     repo.full_name)
    else:
        LOG.info("Mergify already configured for repo %s", repo.full_name)
