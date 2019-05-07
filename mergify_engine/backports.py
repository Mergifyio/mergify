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

from mergify_engine import config
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
        LOG.error("backport failed: %s", e.output,
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

    return repo.create_pull(
        title="[bp] %s" % pull.g_pull.title,
        body=body,
        base=branch.name,
        head=bp_branch,
    )
