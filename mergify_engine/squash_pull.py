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

from mergify_engine import context
from mergify_engine.clients import http
from mergify_engine.user_tokens import UserTokensUser
from mergify_engine import gitter
from mergify_engine import config


@dataclasses.dataclass
class SquashFailure(Exception):
    reason: str


async def _do_squash(ctxt: context.Context, user: UserTokensUser, title: str) -> None:

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
        # https://github.com + /mergify-test2/head_branch

        await git("remote", "add", "upstream", f"{config.GITHUB_URL}/{base_repo}")
        # https://github.com + /mergify-testing/base_branch

        # récupérer la branche de base
        await git("fetch", "-q", "upstream", base_branch)
        await git("checkout", "-q", "-b", base_branch, f"upstream/{base_branch}")

        # récupérer la branche head
        await git("fetch", "-q", "origin", head_branch)
        await git("checkout", "-q", "-b", head_branch, f"origin/{head_branch}")

        # se replacer sur la branche de base
        await git("checkout", base_branch)
        # checkout 20210531172937/test_squash_several_commits_ok/master

        # merger head dans base en utilisant squash
        await git("merge", "--squash", "--no-edit", head_branch)
        await git("commit", "-m", title)

        # envoyer la branche à jour sur head
        await git("push", "--verbose", "origin", base_branch, "--force")

    except gitter.GitError as e:  # pragma: no cover
        # TODO: à revoir
        # raise SquashFailure(f"Error: {e}")
        with open("log.txt", "a") as f:
            f.write(f"{e}\n")

    finally:
        await git.cleanup()


async def squash_pull(ctxt: context.Context) -> None:

    if ctxt.pull["commits"] <= 1:
        return

    pull_number = ctxt.pull["number"]

    try:
        # get commits' pull to have its first and last commit
        commits = [
            commit["commit"]
            async for commit in ctxt.client.items(
                f"{ctxt.base_url}/pulls/{pull_number}/commits"
            )
        ]

        # get the title of the first commit
        message = commits[0]["message"]
        title = message[: message.find("\n\n")]

        # get users
        user_tokens = await ctxt.repository.installation.get_user_tokens()
        users = user_tokens.users

        # Pick author first
        users = sorted(users, key=lambda x: x["login"] != ctxt.pull["user"]["login"])

        if not users:
            return

        for user in users:
            await _do_squash(ctxt, user, title)

    except http.HTTPClientSideError as e:
        raise SquashFailure(f"Get commits: {e.message}")
