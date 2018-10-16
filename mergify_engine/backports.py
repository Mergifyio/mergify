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
import subprocess

import daiquiri

import github

from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


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
        LOG.info("was rebased before being merged", pull_request=pull)
        # NOTE(sileht): Was rebased before merge
        # so we can't use the commit from the PR
        # the SHAs in the branch are not the same
        # So browse the branch to get the commit to
        # cherry-pick
        out_commits = []
        for _ in range(len(commits)):
            if len(commit.parents) == 1:
                out_commits.insert(0, commit)
                commit = commit.parents[0]
            else:  # pragma: no cover
                # NOTE(sileht): What is that? A merge here?
                LOG.error("unhandled commit structure",
                          pull_request=pull)
                return []
        return out_commits

    elif len(commit.parents) == 2:
        LOG.info("just merged", pull_request=pull)
        # NOTE(sileht): Was merged, we can take commit from the PR
        return commits
    else:  # pragma: no cover
        # NOTE(sileht): What is that?
        LOG.error("unhandled commit structure", pull_request=pull)
        return []


def backport(pull, branch_name, installation_token):
    """Backport a pull request.

    :param repo: The repository.
    :param pull: The pull request.
    :type pull: py:class:mergify_engine.mergify_pull.MergifyPull
    :param branch_name: The branch name to backport to.
    :param installation_token: The installation token.
    """
    repo = pull.g_pull.base.repo

    try:
        branch = repo.get_branch(branch_name)
    except github.GithubException as e:
        # NOTE(sileht): PyGitHub is buggy here it should
        # UnknownObjectException. but because the message is "Branch not
        # found", instead of "Not found", we get the generic exception.
        if e.status != 404:  # pragma: no cover
            raise

        LOG.info("branch doesn't exist for repository",
                 branch=branch_name,
                 repository=repo.full_name)
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
        git("init")
        git.configure()
        git.add_cred("x-access-token", installation_token, repo.full_name)
        git("remote", "add", "origin",
            "https://github.com/%s" % repo.full_name)

        git("fetch", "--quiet", "origin", "pull/%s/head" % pull.g_pull.number)
        git("fetch", "--quiet", "origin", pull.g_pull.base.ref)
        git("fetch", "--quiet", "origin", branch_name)
        git("checkout", "--quiet", "-b", bp_branch, "origin/%s" % branch_name)

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
                git("commit", "-a", "--no-edit", "--allow-empty")

                body += ("\n\nCherry-pick of %s have failed:\n```\n%s```\n\n"
                         % (commit.sha, status))

        git("push", "origin", bp_branch)
    except Exception:  # pragma: no cover
        LOG.error("backport failed", pull_request=pull, branch=branch.name,
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

    return repo.create_pull(
        title="Automatic backport of pull request #%d" % pull.g_pull.number,
        body=body,
        base=branch.name,
        head=bp_branch,
    )


def backport_from_labels(pull, labels_branches, installation_token):
    if not labels_branches:
        return

    labels = (set(labels_branches) & set(l.name for l in pull.g_pull.labels))
    for l in labels:
        backport(pull, labels_branches[l], installation_token)
