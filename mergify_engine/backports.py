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

from mergify_engine import config
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


ERRORS_TO_IGNORE = set([
    "reference already exists",
    "You may want to first integrate the remote changes",
])


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


def is_base_branch_merge_commit(commit, base_branch):
    return (
        commit.commit.message.startswith(
            "Merge branch '%s'" % base_branch
        ) and len(commit.parents) == 2
    )


def _get_commits_without_base_branch_merge(pull):
    commits = pull.g_pull.get_commits()
    base_branch = pull.g_pull.base.ref
    return list(filter(
        lambda c: not is_base_branch_merge_commit(c, base_branch),
        sorted(commits, key=CommitOrderingKey)
    ))


def _get_commits_to_cherrypick(pull, merge_commit):
    if len(merge_commit.parents) == 1:
        # NOTE(sileht): We have a rebase+merge or squash+merge
        # We pick all commits until a sha is not linked with our PR

        out_commits = []
        commit = merge_commit
        while True:
            if len(commit.parents) != 1:
                # NOTE(sileht): What is that? A merge here?
                LOG.error("unhandled commit structure",
                          pull_request=pull)
                return []

            out_commits.insert(0, commit)
            commit = commit.parents[0]
            pulls = utils.get_github_pulls_from_sha(
                pull.g_pull.base.repo, commit.sha)
            pull_numbers = [p.number for p in pulls]

            if pull.g_pull.number not in pull_numbers:
                if len(out_commits) == 1:
                    LOG.info("Pull requests merged with one commit rebased, "
                             "or squashed", pull_request=pull)
                else:
                    LOG.info("Pull requests merged after rebase",
                             pull_request=pull)
                return out_commits

    elif len(merge_commit.parents) == 2:
        LOG.info("Pull request merged with merge commit", pull_request=pull)
        return _get_commits_without_base_branch_merge(pull)

    else:  # pragma: no cover
        # NOTE(sileht): What is that?
        LOG.error("unhandled commit structure", pull_request=pull)
        return []


def backport(pull, branch, installation_token):
    """Backport a pull request.

    :param repo: The repository.
    :param pull: The pull request.
    :type pull: py:class:mergify_engine.mergify_pull.MergifyPull
    :param branch_name: The branch name to backport to.
    :param installation_token: The installation token.
    """
    repo = pull.g_pull.base.repo

    bp_branch = "mergify/bp/%s/pr-%s" % (branch.name, pull.g_pull.number)

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
        git("remote", "add", "origin", "https://%s/%s" % (config.GITHUB_DOMAIN,
                                                          repo.full_name))

        git("fetch", "--quiet", "origin", "pull/%s/head" % pull.g_pull.number)
        git("fetch", "--quiet", "origin", pull.g_pull.base.ref)
        git("fetch", "--quiet", "origin", branch.name)
        git("checkout", "--quiet", "-b", bp_branch, "origin/%s" % branch.name)

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
            except subprocess.CalledProcessError as e:  # pragma: no cover
                LOG.debug("fail to cherry-pick %s: %s", commit.sha, e.output)
                cherry_pick_fail = True
                status = git("status").decode("utf8")
                git("add", "*")
                git("commit", "-a", "--no-edit", "--allow-empty")

                body += ("\n\nCherry-pick of %s has failed:\n```\n%s```\n\n"
                         % (commit.sha, status))

        git("push", "origin", bp_branch)
    except subprocess.CalledProcessError as e:  # pragma: no cover
        output = e.output.decode()
        for error in ERRORS_TO_IGNORE:
            if error in output:
                return
        LOG.error("backport failed: %s", output,
                  pull_request=pull, branch=branch.name,
                  exc_info=True)
        return
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

    try:
        return repo.create_pull(
            title="{} (bp #{})".format(pull.g_pull.title, pull.g_pull.number),
            body=body,
            base=branch.name,
            head=bp_branch,
        )
    except github.GithubException as e:
        if e.status == 422 and "No commits between" in e.data["message"]:
            return
        raise
