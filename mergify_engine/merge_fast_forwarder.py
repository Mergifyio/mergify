# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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

from mergify_engine import exceptions
from mergify_engine import gitter


if typing.TYPE_CHECKING:
    from mergify_engine import context
    from mergify_engine.dashboard import user_tokens


@dataclasses.dataclass
class MergeFastForwardUnexpectedError(Exception):
    reason: str


@dataclasses.dataclass
class MergeFastForwardNeedRetry(exceptions.EngineNeedRetry):
    reason: str


@dataclasses.dataclass
class MergeFastForwardFailed(Exception):
    reason: str


@dataclasses.dataclass
class MergeFastForwardBaseMoved(Exception):
    reason: str


GIT_MESSAGE_TO_EXCEPTION = {
    "(non-fast-forward)": MergeFastForwardFailed,
    "Updates were rejected because the tip of your current branch is behind": MergeFastForwardBaseMoved,
    "You may want to first integrate the remote changes": MergeFastForwardBaseMoved,
}


async def merge(
    ctxt: "context.Context",
    bot_account_user: typing.Optional["user_tokens.UserTokensUser"],
) -> None:
    if ctxt.pull["head"]["repo"] is None:
        raise MergeFastForwardFailed("The head repository does not exists anymore")

    head_branch = ctxt.pull["head"]["ref"]
    base_branch = ctxt.pull["base"]["ref"]
    git = gitter.Gitter(ctxt.log)
    try:
        await git.init()

        if bot_account_user is None:
            token = await ctxt.client.get_access_token()
            await git.configure()
            username = "x-access-token"
            password = token
        else:
            await git.configure(
                bot_account_user["name"] or bot_account_user["login"],
                bot_account_user["email"],
            )
            username = bot_account_user["oauth_access_token"]
            password = ""

        await git.setup_remote("head", ctxt.pull["head"]["repo"], username, password)
        await git.setup_remote("base", ctxt.pull["base"]["repo"], username, password)

        await git("fetch", "--quiet", "head", head_branch)
        await git("fetch", "--quiet", "base", base_branch)

        await git("checkout", "-q", "-b", base_branch, f"base/{base_branch}")
        await git("merge", "--ff-only", f"head/{head_branch}")
        await git("push", "--verbose", "base", base_branch, "--force-with-lease")

    except gitter.GitError as e:  # pragma: no cover
        exception_reason = f"Git reported the following error:\n```\n{e.output}\n```\n"
        if isinstance(e, gitter.GitAuthenticationFailure):
            if bot_account_user is None:
                raise MergeFastForwardNeedRetry(exception_reason)
            else:
                raise MergeFastForwardUnexpectedError(exception_reason)
        elif isinstance(e, gitter.GitErrorRetriable):
            raise MergeFastForwardNeedRetry(exception_reason)
        elif isinstance(e, gitter.GitFatalError):
            raise MergeFastForwardUnexpectedError(exception_reason)

        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in e.output:
                raise out_exception(exception_reason)
        ctxt.log.error(
            "fast-forward merge has failed",
            output=e.output,
            returncode=e.returncode,
            exc_info=True,
        )
        raise MergeFastForwardUnexpectedError(e.output)
    finally:
        await git.cleanup()
