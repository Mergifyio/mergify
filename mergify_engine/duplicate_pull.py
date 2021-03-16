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
import typing

import tenacity

from mergify_engine import config
from mergify_engine import context
from mergify_engine import doc
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import gitter
from mergify_engine import subscription
from mergify_engine.clients import http


@dataclasses.dataclass
class DuplicateAlreadyExists(Exception):
    reason: str


@dataclasses.dataclass
class DuplicateUnexpectedError(Exception):
    reason: str


@dataclasses.dataclass
class DuplicateNeedRetry(exceptions.EngineNeedRetry):
    reason: str


@dataclasses.dataclass
class DuplicateNotNeeded(Exception):
    reason: str


@dataclasses.dataclass
class DuplicateFailed(Exception):
    reason: str


GIT_MESSAGE_TO_EXCEPTION = {
    "No such device or address": DuplicateNeedRetry,
    "Could not resolve host": DuplicateNeedRetry,
    "Authentication failed": DuplicateNeedRetry,
    "remote end hung up unexpectedly": DuplicateNeedRetry,
    "Operation timed out": DuplicateNeedRetry,
    "reference already exists": DuplicateAlreadyExists,
    "Aborting commit due to empty commit message": DuplicateNotNeeded,
    "You may want to first integrate the remote changes": DuplicateNeedRetry,
}


@functools.total_ordering
class CommitOrderingKey:
    def __init__(self, obj: github_types.GitHubBranchCommit) -> None:
        self.obj = obj

    @staticmethod
    def order_commit(
        c1: github_types.GitHubBranchCommit, c2: github_types.GitHubBranchCommit
    ) -> int:
        if c1["sha"] == c2["sha"]:
            return 0

        for p in c1["parents"]:
            if c2["sha"] == p["sha"]:
                return 1

        return -1

    def __lt__(self, other: "CommitOrderingKey") -> bool:
        return (
            isinstance(other, CommitOrderingKey)
            and self.order_commit(self.obj, other.obj) < 0
        )

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, CommitOrderingKey)
            and self.order_commit(self.obj, other.obj) == 0
        )


def is_base_branch_merge_commit(
    commit: github_types.GitHubBranchCommit, base_branch: github_types.GitHubRefType
) -> bool:
    return (
        commit["commit"]["message"].startswith(f"Merge branch '{base_branch}'")
        and len(commit["parents"]) == 2
    )


async def _get_commits_without_base_branch_merge(
    ctxt: context.Context,
) -> typing.List[github_types.GitHubBranchCommit]:
    base_branch = ctxt.pull["base"]["ref"]
    return list(
        filter(
            lambda c: not is_base_branch_merge_commit(c, base_branch),
            sorted(await ctxt.commits, key=CommitOrderingKey),
        )
    )


async def _get_commits_to_cherrypick(
    ctxt: context.Context, merge_commit: github_types.GitHubBranchCommit
) -> typing.List[github_types.GitHubBranchCommit]:
    if len(merge_commit["parents"]) == 1:
        # NOTE(sileht): We have a rebase+merge or squash+merge
        # We pick all commits until a sha is not linked with our PR

        out_commits: typing.List[github_types.GitHubBranchCommit] = []
        commit = merge_commit
        while True:
            if len(commit["parents"]) != 1:
                # NOTE(sileht): What is that? A merge here?
                ctxt.log.error("unhandled commit structure")
                return []

            out_commits.insert(0, commit)
            parent_commit = commit["parents"][0]

            pull_numbers = [
                p["number"]
                async for p in ctxt.client.items(
                    f"{ctxt.base_url}/commits/{parent_commit['sha']}/pulls",
                    api_version="groot",
                )
                if (
                    p["base"]["repo"]["full_name"]
                    == ctxt.pull["base"]["repo"]["full_name"]
                )
            ]

            # Head repo can be None if deleted in the meantime
            if ctxt.pull["head"]["repo"] is not None:
                pull_numbers += [
                    p["number"]
                    async for p in ctxt.client.items(
                        f"/repos/{ctxt.pull['head']['repo']['full_name']}/commits/{parent_commit['sha']}/pulls",
                        api_version="groot",
                    )
                    if (
                        p["base"]["repo"]["full_name"]
                        == ctxt.pull["base"]["repo"]["full_name"]
                    )
                ]

            if ctxt.pull["number"] not in pull_numbers:
                if len(out_commits) == 1:
                    ctxt.log.debug(
                        "Pull requests merged with one commit rebased, or squashed",
                    )
                else:
                    ctxt.log.debug("Pull requests merged after rebase")
                return out_commits

            # Prepare next iteration
            commit = typing.cast(
                github_types.GitHubBranchCommit,
                await ctxt.client.item(
                    f"{ctxt.base_url}/commits/{parent_commit['sha']}"
                ),
            )

    elif len(merge_commit["parents"]) == 2:
        ctxt.log.debug("Pull request merged with merge commit")
        return await _get_commits_without_base_branch_merge(ctxt)

    else:  # pragma: no cover
        # NOTE(sileht): What is that?
        ctxt.log.error("unhandled commit structure")
        return []


KindT = typing.Literal["backport", "copy"]
BACKPORT = "backport"
COPY = "copy"

BRANCH_PREFIX_MAP = {BACKPORT: "bp", COPY: "copy"}


def get_destination_branch_name(
    pull_number: github_types.GitHubPullRequestNumber, branch_name: str, kind: KindT
) -> str:
    return f"mergify/{BRANCH_PREFIX_MAP[kind]}/{branch_name}/pr-{pull_number}"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(DuplicateNeedRetry),
    reraise=True,
)
async def duplicate(
    ctxt: context.Context,
    branch_name: str,
    label_conflicts: typing.Optional[str] = None,
    ignore_conflicts: bool = False,
    kind: KindT = "backport",
) -> typing.Optional[github_types.GitHubPullRequest]:
    """Duplicate a pull request.

    :param pull: The pull request.
    :type pull: py:class:mergify_engine.context.Context
    :param branch: The branch to copy to.
    :param label_conflicts: The label to add to the created PR when cherry-pick failed.
    :param ignore_conflicts: Whether to commit the result if the cherry-pick fails.
    :param kind: is a backport or a copy
    """
    repo_full_name = ctxt.pull["base"]["repo"]["full_name"]
    bp_branch = get_destination_branch_name(ctxt.pull["number"], branch_name, kind)

    cherry_pick_fail = False
    body = ""

    repo_info = await ctxt.client.item(f"/repos/{repo_full_name}")
    if repo_info["size"] > config.NOSUB_MAX_REPO_SIZE_KB:
        if not ctxt.subscription.has_feature(subscription.Features.LARGE_REPOSITORY):
            ctxt.log.warning(
                "repository too big and no subscription active, refusing to %s",
                kind,
                size=repo_info["size"],
            )
            raise DuplicateFailed(
                f"{kind} fail: repository is too big and no subscription is active"
            )
        ctxt.log.info("running %s on large repository", kind)

    # TODO(sileht): This can be done with the Github API only I think:
    # An example:
    # https://github.com/shiqiyang-okta/ghpick/blob/master/ghpick/cherry.py
    git = gitter.Gitter(ctxt.log)
    try:
        token = ctxt.client.auth.get_access_token()
        await git.init()
        await git.configure()
        await git.add_cred("x-access-token", token, repo_full_name)
        await git("remote", "add", "origin", f"{config.GITHUB_URL}/{repo_full_name}")
        await git("fetch", "--quiet", "origin", f"pull/{ctxt.pull['number']}/head")
        await git("fetch", "--quiet", "origin", ctxt.pull["base"]["ref"])
        await git("fetch", "--quiet", "origin", branch_name)
        await git("checkout", "--quiet", "-b", bp_branch, f"origin/{branch_name}")

        merge_commit = await ctxt.client.item(
            f"{ctxt.base_url}/commits/{ctxt.pull['merge_commit_sha']}"
        )
        for commit in await _get_commits_to_cherrypick(ctxt, merge_commit):
            # FIXME(sileht): Github does not allow to fetch only one commit
            # So we have to fetch the branch since the commit date ...
            # git("fetch", "origin", "%s:refs/remotes/origin/%s-commit" %
            #    (commit["sha"], commit["sha"])
            #    )
            # last_commit_date = commit["commit"]["committer"]["date"]
            # git("fetch", "origin", ctxt.pull["base"]["ref"],
            #    "--shallow-since='%s'" % last_commit_date)
            try:
                await git("cherry-pick", "-x", commit["sha"])
            except gitter.GitError as e:  # pragma: no cover
                ctxt.log.info("fail to cherry-pick %s: %s", commit["sha"], e.output)
                output = await git("status")
                body += f"\n\nCherry-pick of {commit['sha']} has failed:\n```\n{output}```\n\n"
                if not ignore_conflicts:
                    raise DuplicateFailed(body)
                cherry_pick_fail = True
                await git("add", "*")
                await git("commit", "-a", "--no-edit", "--allow-empty")

        await git("push", "origin", bp_branch)
    except gitter.GitError as in_exception:  # pragma: no cover
        if in_exception.output == "":
            raise DuplicateNeedRetry("git process got sigkill")

        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in in_exception.output:
                raise out_exception(
                    "Git reported the following error:\n"
                    f"```\n{in_exception.output}\n```\n"
                )
        else:
            raise DuplicateUnexpectedError(in_exception.output)
    finally:
        await git.cleanup()

    body = (
        f"This is an automatic {kind} of pull request #{ctxt.pull['number']} done by [Mergify](https://mergify.io)."
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
        duplicate_pr = typing.cast(
            github_types.GitHubPullRequest,
            (
                await ctxt.client.post(
                    f"{ctxt.base_url}/pulls",
                    json={
                        "title": f"{ctxt.pull['title']} ({BRANCH_PREFIX_MAP[kind]} #{ctxt.pull['number']})",
                        "body": body + "\n\n---\n\n" + doc.MERGIFY_PULL_REQUEST_DOC,
                        "base": branch_name,
                        "head": bp_branch,
                    },
                )
            ).json(),
        )
    except http.HTTPClientSideError as e:
        if e.status_code == 422 and "No commits between" in e.message:
            raise DuplicateNotNeeded(e.message)
        raise

    if cherry_pick_fail and label_conflicts is not None:
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{duplicate_pr['number']}/labels",
            json={"labels": [label_conflicts]},
        )

    return duplicate_pr
