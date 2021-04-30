# -*- encoding: utf-8 -*-
#
#  Copyright © 2021 Mergify SAS
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

from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import gitter
from mergify_engine import user_tokens


@dataclasses.dataclass
class SquashFailure(Exception):
    reason: str


@dataclasses.dataclass
class SquashNeedRetry(exceptions.EngineNeedRetry):
    message: str


async def _do_squash(
    ctxt: context.Context, user: user_tokens.UserTokensUser, squash_message: str
) -> None:

    head_branch = ctxt.pull["head"]["ref"]
    base_branch = ctxt.pull["base"]["ref"]
    tmp_branch = "squashed-head-branch"

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
        await git("fetch", "--quiet", "upstream", base_branch)
        await git("checkout", "-q", "-b", tmp_branch, f"upstream/{base_branch}")

        await git("merge", "--squash", "--no-edit", f"origin/{head_branch}")
        await git("commit", "-m", squash_message)

        await git(
            "push",
            "--verbose",
            "origin",
            f"{tmp_branch}:{head_branch}",
            "--force-with-lease",
        )

        expected_sha = (await git("log", "-1", "--format=%H")).strip()
        # NOTE(sileht): We store this for dismissal action
        # FIXME(sileht): use a more generic name for the key
        await ctxt.redis.setex(f"branch-update-{expected_sha}", 60 * 60, expected_sha)
    except gitter.GitAuthenticationFailure:
        raise
    except gitter.GitErrorRetriable as e:
        raise SquashNeedRetry(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitFatalError as e:
        raise SquashFailure(
            f"Git reported the following error:\n```\n{e.output}\n```\n"
        )
    except gitter.GitError as e:
        ctxt.log.error(
            "squash failed",
            output=e.output,
            returncode=e.returncode,
            exc_info=True,
        )
        raise SquashFailure("")
    except Exception:  # pragma: no cover
        ctxt.log.error("squash failed", exc_info=True)
        raise SquashFailure("")
    finally:
        await git.cleanup()


async def squash(
    ctxt: context.Context,
    message: str,
    bot_account: typing.Optional[str] = None,
) -> None:

    if ctxt.pull["commits"] <= 1:
        return

    try:
        users = await user_tokens.UserTokens.select_users_for(ctxt, bot_account)
    except user_tokens.UserTokensUserNotFound as e:
        raise SquashFailure(f"Unable to squash: {e.reason}")

    for user in users:
        try:
            await _do_squash(ctxt, user, message)
        except gitter.GitAuthenticationFailure as e:
            ctxt.log.info(
                "authentification failure, will retry another token: %s",
                e,
                login=user["login"],
            )
        else:
            return

    ctxt.log.warning("unable to squash pull request: no tokens are valid")

    if ctxt.pull_from_fork and ctxt.pull["base"]["repo"]["private"]:
        raise SquashFailure(
            "Squashing a branch for a forked private repository is not supported by GitHub"
        )

    raise SquashFailure("No oauth valid tokens")
