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
import collections
import dataclasses
import typing

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import gitter
from mergify_engine.user_tokens import UserTokensUser


@dataclasses.dataclass
class SquashFailure(Exception):
    reason: str


@dataclasses.dataclass
class SquashNeedRetry(exceptions.EngineNeedRetry):
    message: str


class AuthenticationFailure(Exception):
    pass


GIT_MESSAGE_TO_EXCEPTION = collections.OrderedDict(
    [
        ("This repository was archived so it is read-only.", SquashFailure),
        ("organization has enabled or enforced SAML SSO.", SquashFailure),
        ("could not apply", SquashFailure),
        ("Invalid username or password", AuthenticationFailure),
        ("Repository not found", AuthenticationFailure),
        ("The requested URL returned error: 403", AuthenticationFailure),
        ("Patch failed at", SquashFailure),
        ("remote contains work that you do", SquashNeedRetry),
        ("remote end hung up unexpectedly", SquashNeedRetry),
        ("cannot lock ref 'refs/heads/", SquashNeedRetry),
        ("Could not resolve host", SquashNeedRetry),
        ("Operation timed out", SquashNeedRetry),
        ("No such device or address", SquashNeedRetry),
        ("Protected branch update failed", SquashFailure),
        ("couldn't find remote ref", SquashFailure),
    ]
)


def is_force_push_lease_reject(message: str) -> bool:
    return (
        "failed to push some refs" in message
        and "[rejected]" in message
        and "(stale info)" in message
    )


async def _do_squash(
    ctxt: context.Context, user: UserTokensUser, squash_message: str
) -> None:

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
    tmp_branch = "squashed-head-branch"

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
        await git("fetch", "--quiet", "origin", head_branch)

        await git("remote", "add", "upstream", f"{config.GITHUB_URL}/{base_repo}")
        await git("config", "remote.upstream.promisor", "true")
        await git("config", "remote.upstream.partialclonefilter", "blob:none")
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
    except gitter.GitError as in_exception:  # pragma: no cover
        if in_exception.output == "":
            # SIGKILL...
            raise SquashNeedRetry("Git process got killed")

        elif is_force_push_lease_reject(in_exception.output):
            raise SquashNeedRetry(
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
                "squash failed: %s",
                in_exception.output,
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

    user_tokens = await ctxt.repository.installation.get_user_tokens()
    if bot_account:
        user = user_tokens.get_token_for(bot_account)
        if user:
            users = [user]
        else:
            raise SquashFailure(
                f"Unable to rebase: user `{bot_account}` is unknown. "
                f"Please make sure `{bot_account}` has logged in Mergify dashboard."
            )
    else:
        users = user_tokens.users

    # Pick author first
    users = sorted(users, key=lambda x: x["login"] != ctxt.pull["user"]["login"])

    for user in users:
        try:
            await _do_squash(ctxt, user, message)
        except AuthenticationFailure as e:
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
