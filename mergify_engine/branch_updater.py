# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
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
import collections
import dataclasses
import typing
import uuid

import tenacity

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import gitter
from mergify_engine.clients import http
from mergify_engine.user_tokens import UserTokensUser


class BranchUpdateFailure(Exception):
    def __init__(
        self,
        msg="",
        title="Base branch update has failed",
    ):
        error_code = "err-code: " + uuid.uuid4().hex[-5:].upper()
        self.title = title
        self.message = msg + "\n" + error_code
        super(BranchUpdateFailure, self).__init__(self.message)


@dataclasses.dataclass
class BranchUpdateNeedRetry(exceptions.EngineNeedRetry):
    message: str


class AuthenticationFailure(Exception):
    pass


GIT_MESSAGE_TO_EXCEPTION = collections.OrderedDict(
    [
        ("This repository was archived so it is read-only.", BranchUpdateFailure),
        ("organization has enabled or enforced SAML SSO.", BranchUpdateFailure),
        ("Invalid username or password", AuthenticationFailure),
        ("Repository not found", AuthenticationFailure),
        ("The requested URL returned error: 403", AuthenticationFailure),
        ("Patch failed at", BranchUpdateFailure),
        ("remote contains work that you do", BranchUpdateNeedRetry),
        ("remote end hung up unexpectedly", BranchUpdateNeedRetry),
        ("cannot lock ref 'refs/heads/", BranchUpdateNeedRetry),
        ("Could not resolve host", BranchUpdateNeedRetry),
        ("Operation timed out", BranchUpdateNeedRetry),
        ("No such device or address", BranchUpdateNeedRetry),
        ("Protected branch update failed", BranchUpdateFailure),
        ("couldn't find remote ref", BranchUpdateFailure),
    ]
)


def is_force_push_lease_reject(message: str) -> bool:
    return (
        "failed to push some refs" in message
        and "[rejected]" in message
        and "(stale info)" in message
    )


def pre_update_check(ctxt: context.Context) -> None:
    # If PR from a public fork but cannot be edited
    if (
        ctxt.pull_from_fork
        and not ctxt.pull["base"]["repo"]["private"]
        and not ctxt.pull["maintainer_can_modify"]
    ):
        raise BranchUpdateFailure(
            "Mergify needs the author permission to update the base branch of the pull request.\n"
            f"{ctxt.pull['head']['repo']['owner']['login']} needs to "
            "[authorize modification on its head branch]"
            "(https://help.github.com/articles/allowing-changes-to-a-pull-request-branch-created-from-a-fork/).",
            title="Pull request can't be updated with latest base branch changes",
        )


async def pre_rebase_check(ctxt: context.Context) -> None:
    pre_update_check(ctxt)

    # If PR from a private fork but cannot be edited:
    # NOTE(jd): GitHub removed the ability to configure `maintainer_can_modify` on private
    # fork we which make rebase impossible
    if (
        ctxt.pull_from_fork
        and ctxt.pull["base"]["repo"]["private"]
        and not ctxt.pull["maintainer_can_modify"]
    ):
        raise BranchUpdateFailure(
            "Mergify needs the permission to update the base branch of the pull request.\n"
            "GitHub does not allow a GitHub App to modify base branch for a private fork.\n"
            "You cannot `rebase` a pull request from a private fork.",
            title="Pull request can't be updated with latest base branch changes",
        )
    elif await ctxt.github_workflow_changed():
        raise BranchUpdateFailure(
            "GitHub App like Mergify are not allowed to rebase pull request where `.github/workflows` is changed.\n"
            "This pull request must be rebased manually.",
            title="Pull request can't be updated with latest base branch changes",
        )


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
    reraise=True,
)
async def _do_rebase(ctxt: context.Context, user: UserTokensUser) -> None:
    # NOTE(sileht):
    # $ curl https://api.github.com/repos/sileht/repotest/pulls/2 | jq .commits
    # 2
    # $ git clone https://XXXXX@github.com/sileht-tester/repotest \
    #           --depth=$((2 + 1)) -b sileht/testpr
    # $ cd repotest
    # $ git remote add upstream https://XXXXX@github.com/sileht/repotest.git
    # $ git log | grep Date | tail -1
    # Date:   Fri Mar 30 21:30:26 2018 (10 days ago)
    # $ git fetch upstream master --shallow-since="Fri Mar 30 21:30:26 2018"
    # $ git rebase upstream/master
    # $ git push origin sileht/testpr:sileht/testpr

    head_repo = (
        ctxt.pull["head"]["repo"]["owner"]["login"]
        + "/"
        + ctxt.pull["head"]["repo"]["name"]
    )
    base_repo = (
        ctxt.pull["base"]["repo"]["owner"]["login"]
        + "/"
        + ctxt.pull["base"]["repo"]["name"]
    )

    head_branch = ctxt.pull["head"]["ref"]
    base_branch = ctxt.pull["base"]["ref"]
    git = gitter.Gitter(ctxt.log)
    try:
        await git.init()
        # NOTE(sileht): Bump the repository format. This ensures required
        # extensions (promisor, partialclonefilter) are present in git cli and
        # raise an error if not. Avoiding git cli to fallback to full clone
        # behavior for us.
        await git("config", "core.repositoryformatversion", "1")

        if ctxt.subscription.active:
            await git.configure(user["name"] or user["login"], user["email"])
        else:
            await git.configure()
        await git.add_cred(user["oauth_access_token"], "", head_repo)
        await git.add_cred(user["oauth_access_token"], "", base_repo)

        await git("remote", "add", "origin", f"{config.GITHUB_URL}/{head_repo}")
        await git("config", "remote.origin.promisor", "true")
        await git("config", "remote.origin.partialclonefilter", "blob:none")

        await git("remote", "add", "upstream", f"{config.GITHUB_URL}/{base_repo}")
        await git("config", "remote.upstream.promisor", "true")
        await git("config", "remote.upstream.partialclonefilter", "blob:none")

        await git("fetch", "--quiet", "origin", head_branch)
        await git("checkout", "-q", "-b", head_branch, f"origin/{head_branch}")

        await git("fetch", "--quiet", "upstream", base_branch)

        await git("rebase", f"upstream/{base_branch}")
        await git("push", "--verbose", "origin", head_branch, "--force-with-lease")

        expected_sha = (await git("log", "-1", "--format=%H")).strip()
        # NOTE(sileht): We store this for dismissal action
        await ctxt.redis.setex(f"branch-update-{expected_sha}", 60 * 60, expected_sha)
    except gitter.GitError as in_exception:  # pragma: no cover
        if in_exception.output == "":
            # SIGKILL...
            raise BranchUpdateNeedRetry("Git process got killed")

        elif is_force_push_lease_reject(in_exception.output):
            raise BranchUpdateNeedRetry(
                "Remote branch changed in the meantime: \n"
                f"```\n{in_exception.output}\n```\n"
            )

        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in in_exception.output:
                raise out_exception(
                    "Git reported the following error:\n"
                    f"```\n{in_exception.output}\n```\n"
                )
        else:
            ctxt.log.error(
                "update branch failed: %s",
                in_exception.output,
                exc_info=True,
            )
            raise BranchUpdateFailure()

    except Exception:  # pragma: no cover
        ctxt.log.error("update branch failed", exc_info=True)
        raise BranchUpdateFailure()
    finally:
        await git.cleanup()


async def update_with_api(ctxt: context.Context) -> None:
    pre_update_check(ctxt)
    try:
        await ctxt.client.put(
            f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": ctxt.pull["head"]["sha"]},
        )
    except http.HTTPClientSideError as e:
        if e.status_code == 422:
            refreshed_pull = await ctxt.client.item(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}"
            )
            if refreshed_pull["head"]["sha"] != ctxt.pull["head"]["sha"]:
                ctxt.log.info(
                    "branch updated in the meantime",
                    status_code=e.status_code,
                    error=e.message,
                )
                return
        ctxt.log.info(
            "update branch failed",
            status_code=e.status_code,
            error=e.message,
        )
        raise BranchUpdateFailure(e.message)


async def rebase_with_git(
    ctxt: context.Context, bot_account: typing.Optional[str] = None
) -> None:

    await pre_rebase_check(ctxt)

    user_tokens = await ctxt.repository.installation.get_user_tokens()
    if bot_account:
        user = user_tokens.get_token_for(bot_account)
        if user:
            users = [user]
        else:
            raise BranchUpdateFailure(
                f"Unable to rebase: user `{bot_account}` is unknown. "
                f"Please make sure `{bot_account}` has logged in Mergify dashboard."
            )
    else:
        users = user_tokens.users

    # Pick author first
    users = sorted(users, key=lambda x: x["login"] != ctxt.pull["user"]["login"])

    for user in users:
        try:
            await _do_rebase(ctxt, user)
        except AuthenticationFailure as e:  # pragma: no cover
            ctxt.log.info(
                "authentification failure, will retry another token: %s",
                e,
                login=user["login"],
            )
        else:
            return

    ctxt.log.warning("unable to update branch: no tokens are valid")

    if ctxt.pull_from_fork and ctxt.pull["base"]["repo"]["private"]:
        raise BranchUpdateFailure(
            "Rebasing a branch for a forked private repository is not supported by GitHub"
        )

    raise BranchUpdateFailure("No oauth valid tokens")


async def update(
    method: typing.Literal["merge", "rebase"],
    ctxt: context.Context,
    user: typing.Optional[str] = None,
) -> None:
    if method == "merge":
        await update_with_api(ctxt)
    else:
        await rebase_with_git(ctxt, user)
