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
import dataclasses
import typing
import uuid

import tenacity

from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import gitter
from mergify_engine import user_tokens
from mergify_engine.clients import http


class BranchUpdateFailure(Exception):
    def __init__(
        self,
        msg: str = "",
        title: str = "Base branch update has failed",
    ) -> None:
        error_code = "err-code: " + uuid.uuid4().hex[-5:].upper()
        self.title = title
        self.message = msg + "\n" + error_code
        super(BranchUpdateFailure, self).__init__(self.message)


@dataclasses.dataclass
class BranchUpdateNeedRetry(exceptions.EngineNeedRetry):
    message: str


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
            "(https://docs.github.com/en/github/collaborating-with-pull-requests/working-with-forks/allowing-changes-to-a-pull-request-branch-created-from-a-fork).",
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
    wait=tenacity.wait_exponential(multiplier=0.2),  # type: ignore[no-untyped-call]
    stop=tenacity.stop_after_attempt(5),  # type: ignore[no-untyped-call]
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),  # type: ignore[no-untyped-call]
    reraise=True,
)
async def _do_rebase(ctxt: context.Context, user: user_tokens.UserTokensUser) -> None:
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

    if ctxt.pull["head"]["repo"] is None:
        raise BranchUpdateFailure("The head repository does not exists anymore")

    head_branch = ctxt.pull["head"]["ref"]
    base_branch = ctxt.pull["base"]["ref"]
    git = gitter.Gitter(ctxt.log)
    try:
        await git.init()
        if ctxt.subscription.active:
            await git.configure(user["name"] or user["login"], user["email"])
        else:
            await git.configure()
        await git.setup_remote(
            "origin", ctxt.pull["head"]["repo"], user["oauth_access_token"], ""
        )
        await git.setup_remote(
            "upstream", ctxt.pull["base"]["repo"], user["oauth_access_token"], ""
        )

        await git("fetch", "--quiet", "origin", head_branch)
        await git("checkout", "-q", "-b", head_branch, f"origin/{head_branch}")

        await git("fetch", "--quiet", "upstream", base_branch)

        await git("rebase", f"upstream/{base_branch}")
        await git("push", "--verbose", "origin", head_branch, "--force-with-lease")

        expected_sha = (await git("log", "-1", "--format=%H")).strip()
        # NOTE(sileht): We store this for dismissal action
        await ctxt.redis.setex(f"branch-update-{expected_sha}", 60 * 60, expected_sha)
    except gitter.GitAuthenticationFailure:
        raise
    except gitter.GitErrorRetriable as e:
        raise BranchUpdateNeedRetry(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitFatalError as e:
        raise BranchUpdateFailure(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitError as e:
        ctxt.log.error(
            "update branch failed",
            output=e.output,
            returncode=e.returncode,
            exc_info=True,
        )
        raise BranchUpdateFailure()
    except Exception:  # pragma: no cover
        ctxt.log.error("update branch failed", exc_info=True)
        raise BranchUpdateFailure()
    finally:
        await git.cleanup()


async def update_with_api(ctxt: context.Context) -> None:
    ctxt.log.info("updating base branch with api")
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
    ctxt.log.info("updating base branch with git")

    await pre_rebase_check(ctxt)

    try:
        users = await user_tokens.UserTokens.select_users_for(ctxt, bot_account)
    except user_tokens.UserTokensUserNotFound as e:
        raise BranchUpdateFailure(f"Unable to rebase: {e.reason}")

    for user in users:
        try:
            await _do_rebase(ctxt, user)
        except gitter.GitAuthenticationFailure as e:  # pragma: no cover
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
