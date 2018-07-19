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

import github

from mergify_engine import config
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
    return (commit.commit.message.startswith("Merge branch '") and
            len(commit.parents) == 2)


def _get_commits_to_cherrypick(pull, commit):
    commits = list(filter(lambda c: not is_base_branch_merge_commit(c),
                          sorted(pull.g_pull.get_commits(),
                                 key=CommitOrderingKey)))
    if len(commit.parents) == 1:
        LOG.info("%s: backport, was a rebased before merge", pull)
        # NOTE(sileht): Was rebased before merge
        # so we can't use the commit from the PR
        # the SHAs in the branch are not the same
        # So browse the branch to get the commit to
        # cherry-pick
        out_commits = []
        for _ in range(len(commits)):
            if len(commit.parents) == 1:
                out_commits.append(commit)
                commit = commit.parents[0]
            else:  # pragma: no cover
                # NOTE(sileht): What is that? A merge here?
                LOG.error("%s: backport, unhandled commit structure",
                          pull)
                return []
        return out_commits

    elif len(commit.parents) == 2:
        LOG.info("%s: backport, was just merged", pull)
        # NOTE(sileht): Was merged, we can take commit from the PR
        return commits
    else:  # pragma: no cover
        # NOTE(sileht): What is that ?
        LOG.error("%s: backport, unhandled commit structure", pull)
        return []


def _backport(repo, pull, branch_name, installation_token):
    try:
        branch = repo.get_branch(branch_name)
    except github.GithubException as e:
        # NOTE(sileht): PyGitHub is buggy here it should
        # UnknownObjectException. but because the message is "Branch not
        # found", instead of "Not found", we got the generic exception.
        if e.status != 404:  # pragma: no cover
            raise

        LOG.info("%s doesn't exist for repo %s", branch_name, repo.full_name)
        return

    bp_branch = "mergify/bp/%s/pr-%s" % (branch_name, pull.g_pull.number)

    cherry_pick_fail = False
    body = ("This is an automated backport of pull request #%d done "
            "by Mergify.io" % pull.g_pull.number)

    git = utils.Gitter()

    # TODO(sileht): This can be done with the Github API only I think:
    # An example:
    # https://github.com/shiqiyang-okta/ghpick/blob/master/ghpick/cherry.py
    try:
        git("clone", "-b", branch_name,
            "https://x-access-token:%s@github.com/%s/" %
            (installation_token, repo.full_name), ".")
        git("branch", "-M", bp_branch)
        git("config", "user.name", "%s-bot" % config.CONTEXT)
        git("config", "user.email", config.GIT_EMAIL)
        merge_commit = repo.get_commit(pull.g_pull.merge_commit_sha)
        for commit in _get_commits_to_cherrypick(pull, merge_commit):
            # FIXME(sileht): Github does not allow to fetch only one commit
            # So we have to fetch the branch since the commit date ...
            # git("fetch", "origin", "%s:refs/remotes/origin/%s-commit" %
            #    (commit.sha, commit.sha)
            #    )
            # last_commit_date = commit.commit.committer.date
            # git("fetch", "origin", pull.base.ref,
            #    "--shallow-since='%s'" % last_commit_date)
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
        LOG.error("%s: backport to branch %s fail", pull, branch.name,
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
        title="Automatic backport of pull request #%d" % pull.g_pull.number,
        body=body,
        base=branch.name,
        head=bp_branch,
    )


def backports(repo, pull, labels_branches, installation_token):
    if not labels_branches:
        return

    labels = (set(labels_branches) & set(l.name for l in pull.g_pull.labels))
    for l in labels:
        _backport(repo, pull, labels_branches[l], installation_token)
