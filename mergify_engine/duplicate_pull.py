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

import dataclasses
import functools
import subprocess

import github
import tenacity

from mergify_engine import config
from mergify_engine import doc
from mergify_engine import utils


class DuplicateNeedRetry(Exception):
    pass


@dataclasses.dataclass
class DuplicateFailed(Exception):
    reason: str


GIT_MESSAGE_TO_EXCEPTION = {
    b"No such device or address": DuplicateNeedRetry,
    b"Could not resolve host": DuplicateNeedRetry,
    b"the remote end hung up unexpectedly": DuplicateNeedRetry,
    b"Operation timed out": DuplicateNeedRetry,
    b"reference already exists": None,
    b"Aborting commit due to empty commit message": None,
    b"You may want to first integrate the remote changes": None,
}


@functools.total_ordering
class CommitOrderingKey(object):
    def __init__(self, obj, *args):
        self.obj = obj

    @staticmethod
    def order_commit(c1, c2):
        if c1["sha"] == c2["sha"]:
            return 0

        for p in c1["parents"]:
            if c2["sha"] == p["sha"]:
                return 1

        return -1

    def __lt__(self, other):
        return self.order_commit(self.obj, other.obj) < 0

    def __eq__(self, other):
        return self.order_commit(self.obj, other.obj) == 0


def is_base_branch_merge_commit(commit, base_branch):
    return (
        commit["commit"]["message"].startswith("Merge branch '%s'" % base_branch)
        and len(commit["parents"]) == 2
    )


def _get_commits_without_base_branch_merge(pull):
    base_branch = pull.data["base"]["ref"]
    return list(
        filter(
            lambda c: not is_base_branch_merge_commit(c, base_branch),
            sorted(pull.commits, key=CommitOrderingKey),
        )
    )


def _get_commits_to_cherrypick(pull, merge_commit):
    if len(merge_commit["parents"]) == 1:
        # NOTE(sileht): We have a rebase+merge or squash+merge
        # We pick all commits until a sha is not linked with our PR

        out_commits = []
        commit = merge_commit
        while True:
            if "parents" not in commit:
                commit = pull.client.item(f"commits/{commit['sha']}")

            if len(commit["parents"]) != 1:
                # NOTE(sileht): What is that? A merge here?
                pull.log.error("unhandled commit structure")
                return []

            out_commits.insert(0, commit)
            commit = commit["parents"][0]
            pulls = utils.get_github_pulls_from_sha(
                pull.g_pull.base.repo, commit["sha"]
            )
            pull_numbers = [p.number for p in pulls]

            if pull.number not in pull_numbers:
                if len(out_commits) == 1:
                    pull.log.info(
                        "Pull requests merged with one commit rebased, or squashed",
                    )
                else:
                    pull.log.info("Pull requests merged after rebase")
                return out_commits

    elif len(merge_commit["parents"]) == 2:
        pull.log.info("Pull request merged with merge commit")
        return _get_commits_without_base_branch_merge(pull)

    else:  # pragma: no cover
        # NOTE(sileht): What is that?
        pull.log.error("unhandled commit structure")
        return []


BACKPORT = "backport"
COPY = "copy"

BRANCH_PREFIX_MAP = {BACKPORT: "bp", COPY: "copy"}


def get_destination_branch_name(pull, branch, kind):
    return "mergify/%s/%s/pr-%s" % (BRANCH_PREFIX_MAP[kind], branch.name, pull.number)


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(DuplicateNeedRetry),
)
def duplicate(
    pull, branch, label_conflicts=None, ignore_conflicts=False, kind=BACKPORT
):
    """Duplicate a pull request.

    :param pull: The pull request.
    :type pull: py:class:mergify_engine.mergify_pull.MergifyPull
    :param branch: The branch to copy to.
    :param label_conflicts: The label to add to the created PR when cherry-pick failed.
    :param ignore_conflicts: Whether to commit the result if the cherry-pick fails.
    :param kind: is a backport or a copy
    """
    repo = pull.g_pull.base.repo

    bp_branch = get_destination_branch_name(pull, branch, kind)

    cherry_pick_fail = False
    body = ""

    git = utils.Gitter()

    # TODO(sileht): This can be done with the Github API only I think:
    # An example:
    # https://github.com/shiqiyang-okta/ghpick/blob/master/ghpick/cherry.py
    try:
        git("init")
        git.configure()
        git.add_cred("x-access-token", pull.installation_token, repo.full_name)
        git(
            "remote",
            "add",
            "origin",
            "https://%s/%s" % (config.GITHUB_DOMAIN, repo.full_name),
        )

        git("fetch", "--quiet", "origin", "pull/%s/head" % pull.number)
        git("fetch", "--quiet", "origin", pull.base_ref)
        git("fetch", "--quiet", "origin", branch.name)
        git("checkout", "--quiet", "-b", bp_branch, "origin/%s" % branch.name)

        merge_commit = pull.client.item(f"commits/{pull.merge_commit_sha}")
        for commit in _get_commits_to_cherrypick(pull, merge_commit):
            # FIXME(sileht): Github does not allow to fetch only one commit
            # So we have to fetch the branch since the commit date ...
            # git("fetch", "origin", "%s:refs/remotes/origin/%s-commit" %
            #    (commit["sha"], commit["sha"])
            #    )
            # last_commit_date = commit["commit"]["committer"]["date"]
            # git("fetch", "origin", pull.base.ref,
            #    "--shallow-since='%s'" % last_commit_date)
            try:
                git("cherry-pick", "-x", commit["sha"])
            except subprocess.CalledProcessError as e:  # pragma: no cover
                pull.log.debug("fail to cherry-pick %s: %s", commit["sha"], e.output)
                git_status = git("status").decode("utf8")
                body += f"\n\nCherry-pick of {commit['sha']} has failed:\n```\n{git_status}```\n\n"
                if not ignore_conflicts:
                    raise DuplicateFailed(body)
                cherry_pick_fail = True
                git("add", "*")
                git("commit", "-a", "--no-edit", "--allow-empty")

        git("push", "origin", bp_branch)
    except subprocess.CalledProcessError as in_exception:  # pragma: no cover
        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in in_exception.output:
                if out_exception is None:
                    return
                else:
                    raise out_exception(in_exception.output.decode())
        else:
            pull.log.error(
                "duplicate failed: %s",
                in_exception.output.decode(),
                branch=branch.name,
                kind=kind,
                exc_info=True,
            )
            return
    finally:
        git.cleanup()

    body = (
        f"This is an automated {kind} of pull request #{pull.number} done by Mergify"
        + body
    )

    if cherry_pick_fail:
        body += (
            "To fixup this pull request, you can check out it locally. "
            "See documentation: "
            "https://help.github.com/articles/"
            "checking-out-pull-requests-locally/"
        )

    try:
        pr = repo.create_pull(
            title="{} ({} #{})".format(
                pull.title, BRANCH_PREFIX_MAP[kind], pull.number
            ),
            body=body + "\n---\n\n" + doc.MERGIFY_PULL_REQUEST_DOC,
            base=branch.name,
            head=bp_branch,
        )
    except github.GithubException as e:
        if e.status == 422 and "No commits between" in e.data["message"]:
            return
        raise

    if cherry_pick_fail and label_conflicts is not None:
        pr.add_to_labels(label_conflicts)

    return pr
