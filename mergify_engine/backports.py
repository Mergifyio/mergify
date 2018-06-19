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

import functools
import logging
import subprocess
import sys

import github

from mergify_engine import config
from mergify_engine import gh_pr
from mergify_engine import utils

LOG = logging.getLogger(__name__)


@functools.total_ordering
class CommitOrderingKey(object):
    def __init__(self, obj, *args):
        self.obj = obj

    @staticmethod
    def order_commit(c1, c2):
        if c1.sha == c2.sha:
            return 0

        for p in c1.parents:
            if c2.sha == p.sha:
                return 1

        return -1

    def __lt__(self, other):
        return self.order_commit(self.obj, other.obj) < 0

    def __eq__(self, other):
        return self.order_commit(self.obj, other.obj) == 0


def is_base_branch_merge_commit(commit):
    return (commit.commit.author.name == "%s-bot" % config.CONTEXT and
            commit.commit.committer.name == "%s-bot" % config.CONTEXT)


def _get_commits_to_cherrypick(pull, commit):
    if len(commit.parents) == 1:
        LOG.info("%s: backport, was a rebased before merge", pull.pretty())
        # NOTE(sileht): Was rebased before merge
        # so we can't use the commit from the PR
        # the SHAs in the branch are not the same
        # So browse the branch to get the commit to
        # cherry-pick
        out_commits = []
        for n in range(pull.commits):
            if len(commit.parents) == 1:
                out_commits.append(commit)
                commit = commit.parents[0]
            elif (len(commit.parents) == 2 and
                  is_base_branch_merge_commit(commit)):
                # NOTE(sileht): Skip 'Merge branch "<base-branch>" into
                # "blabla"' done by Mergify-bot
                commit = commit.parents[0]
            else:
                # NOTE(sileht): What is that? A merge here?
                LOG.error("%s: backport, unhandled commit structure",
                          pull.pretty())
                return []
        return out_commits

    elif len(commit.parents) == 2:
        LOG.info("%s: backport, was just merged", pull.pretty())
        # NOTE(sileht): Was merged, we can take commit from the PR
        return list(filter(lambda c: not is_base_branch_merge_commit(c),
                           sorted(pull.get_commits(), key=CommitOrderingKey)))
    else:
        # NOTE(sileht): What is that ?
        LOG.error("%s: backport, unhandled commit structure", pull.pretty())
        return []


def _backport(repo, pull, branch_name, installation_token):
    try:
        branch = repo.get_branch(branch_name)
    except github.UnknownObjectException:
        LOG.info("%s doesn't exist for repo %s", branch_name, repo.full_name)
        return

    bp_branch = "mergify/bp/%s/pr-%s" % (branch_name, pull.number)

    cherry_pick_fail = False
    body = ("This is an automated backport of pull request #%d done "
            "by Mergify.io" % pull.number)

    repo.create_git_ref(ref="refs/heads/%s" % bp_branch, sha=branch.commit.sha)

    git = utils.Gitter()

    # TODO(sileht): This can be done with the Github API only I think:
    # An example:
    # https://github.com/shiqiyang-okta/ghpick/blob/master/ghpick/cherry.py
    try:
        git("clone", "--depth=1", "-b", bp_branch,
            "https://x-access-token:%s@github.com/%s/" %
            (installation_token, repo.full_name), ".")
        git("config", "user.name", "%s-bot" % config.CONTEXT)
        git("config", "user.email", config.GIT_EMAIL)

        merge_commit = repo.get_commit(pull.merge_commit_sha)
        for commit in _get_commits_to_cherrypick(pull, merge_commit):
            # FIXME(sileht): Github does not allow to fetch only one commit
            # So we have to fetch the branch since the commit date ...
            # git("fetch", "origin", "%s:refs/remotes/origin/%s-commit" %
            #    (commit.sha, commit.sha)
            #    )
            last_commit_date = commit.commit.committer.date
            git("fetch", "origin", pull.base.ref,
                "--shallow-since='%s'" % last_commit_date)
            try:
                git("cherry-pick", "-x", commit.sha)
            except subprocess.CalledProcessError:
                cherry_pick_fail = True
                status = git("status").decode("utf8")
                git("add", "*")
                git("commit", "-a", "--no-edit")

                body += ("\n\nCherry-pick of %s have failed:\n```\n%s```\n\n"
                         % (commit.sha, status))

        git("push", "origin", bp_branch)
    except Exception:
        LOG.error("%s: backport to branch %s fail", pull.pretty(), branch.name,
                  exc_info=True)
        return
    finally:
        git.cleanup()

    if cherry_pick_fail:
        body += (
            "To fixup this pull request, you can check out it locally. "
            "See documentation: "
            "https://help.github.com/articles/"
            "checking-out-pull-requests-locally/")

    repo.create_pull(
        title="Automatic backport of pull request #%d" % pull.number,
        body=body,
        base=branch.name,
        head=bp_branch,
    )


def backports(repo, pull, labels_branches, installation_token):
    if not labels_branches:
        return

    labels = (set(labels_branches) & set(l.name for l in pull.labels))
    for l in labels:
        _backport(repo, pull, labels_branches[l], installation_token)


def test():
    utils.setup_logging()
    config.log()
    gh_pr.monkeypatch_github()

    parts = sys.argv[1].split("/")
    label = sys.argv[2]
    branch = sys.argv[3]

    LOG.info("Getting repo %s ..." % sys.argv[1])

    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)

    installation_id = utils.get_installation_id(integration, parts[3])
    installation_token = integration.get_access_token(installation_id).token
    g = github.Github(installation_token)
    user = g.get_user(parts[3])
    repo = user.get_repo(parts[4])
    pull = repo.get_pull(int(parts[6]))
    backports(repo, pull, {label: branch}, installation_token)


if __name__ == '__main__':
    test()
