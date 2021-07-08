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
from typing import List

import tenacity

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import gitter
from mergify_engine import subscription
from mergify_engine.clients import http
from mergify_engine.user_tokens import UserTokensUser


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
    "Aborting commit due to empty commit message": DuplicateNotNeeded,
    "reference already exists": DuplicateAlreadyExists,
    "You may want to first integrate the remote changes": DuplicateAlreadyExists,
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

    elif len(merge_commit["parents"]) >= 3:
        raise DuplicateFailed("merge commit with more than 2 parents are unsupported")
    else:
        raise RuntimeError("merge commit with no parents")


KindT = typing.Literal["backport", "copy"]


def get_destination_branch_name(
    pull_number: github_types.GitHubPullRequestNumber,
    branch_name: github_types.GitHubRefType,
    branch_prefix: str,
) -> str:
    return f"mergify/{branch_prefix}/{branch_name}/pr-{pull_number}"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),  # type: ignore[no-untyped-call]
    stop=tenacity.stop_after_attempt(5),  # type: ignore[no-untyped-call]
    retry=tenacity.retry_if_exception_type(DuplicateNeedRetry),  # type: ignore[no-untyped-call]
    reraise=True,
)
async def duplicate(
    ctxt: context.Context,
    branch_name: github_types.GitHubRefType,
    *,
    title_template: str,
    body_template: str,
    bot_account: typing.Optional[str] = None,
    labels: typing.Optional[List[str]] = None,
    label_conflicts: typing.Optional[str] = None,
    ignore_conflicts: bool = False,
    assignees: typing.Optional[List[str]] = None,
    kind: KindT = "backport",
    branch_prefix: str = "bp",
) -> typing.Optional[github_types.GitHubPullRequest]:
    """Duplicate a pull request.

    :param pull: The pull request.
    :type pull: py:class:mergify_engine.context.Context
    :param title_template: The pull request title template.
    :param body_template: The pull request body template.
    :param branch: The branch to copy to.
    :param labels: The list of labels to add to the created PR.
    :param label_conflicts: The label to add to the created PR when cherry-pick failed.
    :param ignore_conflicts: Whether to commit the result if the cherry-pick fails.
    :param assignees: The list of users to be assigned to the created PR.
    :param kind: is a backport or a copy
    :param branch_prefix: the prefix of the temporary created branch
    """
    repo_full_name = ctxt.pull["base"]["repo"]["full_name"]
    bp_branch = get_destination_branch_name(
        ctxt.pull["number"], branch_name, branch_prefix
    )

    cherry_pick_error: str = ""

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

    bot_account_user: typing.Optional[UserTokensUser] = None
    if bot_account is not None:
        user_tokens = await ctxt.repository.installation.get_user_tokens()
        bot_account_user = user_tokens.get_token_for(bot_account)
        if not bot_account_user:
            raise DuplicateFailed(
                f"{kind} fail: user `{bot_account}` is unknown. "
                f"Please make sure `{bot_account}` has logged in Mergify dashboard."
            )

    # TODO(sileht): This can be done with the Github API only I think:
    # An example:
    # https://github.com/shiqiyang-okta/ghpick/blob/master/ghpick/cherry.py
    git = gitter.Gitter(ctxt.log)
    try:
        await git.init()

        if bot_account_user is None:
            token = ctxt.client.auth.get_access_token()
            await git.configure()
            username = "x-access-token"
            password = token
        else:
            await git.configure(
                bot_account_user["name"] or bot_account_user["login"],
                bot_account_user["email"],
            )
            username = bot_account_user["oauth_access_token"]
            password = ""  # nosec

        await git.setup_remote("origin", ctxt.pull["base"]["repo"], username, password)

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
                cherry_pick_error += f"Cherry-pick of {commit['sha']} has failed:\n```\n{output}```\n\n\n"
                if not ignore_conflicts:
                    raise DuplicateFailed(cherry_pick_error)
                await git("add", "*")
                await git("commit", "-a", "--no-edit", "--allow-empty")

        await git("push", "origin", bp_branch)
    except gitter.GitAuthenticationFailure:
        raise
    except gitter.GitErrorRetriable as e:
        raise DuplicateNeedRetry(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitFatalError as e:
        raise DuplicateUnexpectedError(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitError as e:  # pragma: no cover
        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in output:
                raise out_exception(
                    f"Git reported the following error:\n```\n{e.output}\n```\n"
                )
        ctxt.log.error(
            "duplicate pull failed",
            output=e.output,
            returncode=e.returncode,
            exc_info=True,
        )
        raise DuplicateUnexpectedError(e.output)
    finally:
        await git.cleanup()

    if cherry_pick_error:
        cherry_pick_error += (
            "To fix up this pull request, you can check it out locally. "
            "See documentation: "
            "https://docs.github.com/en/github/"
            "collaborating-with-pull-requests/reviewing-changes-in-pull-requests/checking-out-pull-requests-locally"
        )

    try:
        title = await ctxt.pull_request.render_template(
            title_template,
            extra_variables={"destination_branch": branch_name},
        )
    except context.RenderTemplateFailure as rmf:
        raise DuplicateFailed(f"Invalid title message: {rmf}")

    try:
        body = await ctxt.pull_request.render_template(
            body_template,
            extra_variables={
                "destination_branch": branch_name,
                "cherry_pick_error": cherry_pick_error,
            },
        )
    except context.RenderTemplateFailure as rmf:
        raise DuplicateFailed(f"Invalid title message: {rmf}")

    try:
        duplicate_pr = typing.cast(
            github_types.GitHubPullRequest,
            (
                await ctxt.client.post(
                    f"{ctxt.base_url}/pulls",
                    json={
                        "title": title,
                        "body": body
                        + "\n\n---\n\n"
                        + constants.MERGIFY_PULL_REQUEST_DOC,
                        "base": branch_name,
                        "head": bp_branch,
                    },
                    oauth_token=bot_account_user["oauth_access_token"]
                    if bot_account_user
                    else None,
                )
            ).json(),
        )
    except http.HTTPClientSideError as e:
        if e.status_code == 422 and "No commits between" in e.message:
            if cherry_pick_error:
                raise DuplicateFailed(cherry_pick_error)
            else:
                raise DuplicateNotNeeded(e.message)
        raise

    effective_labels = []
    if labels is not None:
        effective_labels.extend(labels)

    if cherry_pick_error and label_conflicts is not None:
        effective_labels.append(label_conflicts)

    if len(effective_labels) > 0:
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{duplicate_pr['number']}/labels",
            json={"labels": effective_labels},
        )

    if assignees is not None and len(assignees) > 0:
        # NOTE(sileht): we don't have to deal with invalid assignees as GitHub
        # just ignore them and always return 201
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{duplicate_pr['number']}/assignees",
            json={"assignees": assignees},
        )

    return duplicate_pr
