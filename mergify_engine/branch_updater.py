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
import typing
import uuid

import tenacity

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import gitter
from mergify_engine.clients import http


class BranchUpdateFailure(Exception):
    def __init__(self, msg=""):
        error_code = "err-code: " + uuid.uuid4().hex[-5:].upper()
        self.message = msg + "\n" + error_code
        super(BranchUpdateFailure, self).__init__(self.message)


class BranchUpdateNeedRetry(Exception):
    pass


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
        ("Couldn't find remote ref", BranchUpdateFailure),
    ]
)

GIT_MESSAGE_TO_UNSHALLOW = {"shallow update not allowed", "unrelated histories"}


def pre_rebase_check(ctxt: context.Context) -> typing.Optional[check_api.Result]:
    # If PR from a public fork but cannot be edited
    if (
        ctxt.pull_from_fork
        and not ctxt.pull["base"]["repo"]["private"]
        and not ctxt.pull["maintainer_can_modify"]
    ):
        return check_api.Result(
            check_api.Conclusion.FAILURE,
            "Pull request can't be updated with latest base branch changes",
            "Mergify needs the permission to update the base branch of the pull request.\n"
            f"{ctxt.pull['base']['repo']['owner']['login']} needs to "
            "[authorize modification on its base branch]"
            "(https://help.github.com/articles/allowing-changes-to-a-pull-request-branch-created-from-a-fork/).",
        )
    # If PR from a private fork but cannot be edited:
    # NOTE(jd): GitHub removed the ability to configure `maintainer_can_modify` on private
    # fork we which make rebase impossible
    elif (
        ctxt.pull_from_fork
        and ctxt.pull["base"]["repo"]["private"]
        and not ctxt.pull["maintainer_can_modify"]
    ):
        return check_api.Result(
            check_api.Conclusion.FAILURE,
            "Pull request can't be updated with latest base branch changes",
            "Mergify needs the permission to update the base branch of the pull request.\n"
            "GitHub does not allow a GitHub App to modify base branch for a private fork.\n"
            "You cannot `rebase` a pull request from a private fork.",
        )
    else:
        return None


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
)
async def _do_rebase(ctxt: context.Context, token: str) -> None:
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
        await git.configure()
        await git.add_cred(token, "", head_repo)
        await git.add_cred(token, "", base_repo)
        await git("remote", "add", "origin", f"{config.GITHUB_URL}/{head_repo}")
        await git("remote", "add", "upstream", f"{config.GITHUB_URL}/{base_repo}")

        depth = len(await ctxt.commits) + 1
        await git("fetch", "--quiet", f"--depth={depth}", "origin", head_branch)
        await git("checkout", "-q", "-b", head_branch, f"origin/{head_branch}")

        output = await git("log", "--format=%cI")
        last_commit_date = [d for d in output.split("\n") if d.strip()][-1]

        await git(
            "fetch",
            "--quiet",
            "upstream",
            base_branch,
            f"--shallow-since='{last_commit_date}'",
        )

        # Try to find the merge base, but don't fetch more that 1000 commits.
        for _ in range(20):
            await git("repack", "-d")
            try:
                await git(
                    "merge-base",
                    f"upstream/{base_branch}",
                    f"origin/{head_branch}",
                )
            except gitter.GitError as e:  # pragma: no cover
                if e.returncode == 1:
                    # We need more commits
                    await git("fetch", "-q", "--deepen=50", "upstream", base_branch)
                    continue
                raise
            else:
                break

        try:
            await git("rebase", f"upstream/{base_branch}")
            await git("push", "--verbose", "origin", head_branch, "-f")
        except gitter.GitError as e:  # pragma: no cover
            for message in GIT_MESSAGE_TO_UNSHALLOW:
                if message in e.output:
                    ctxt.log.info("Complete history cloned")
                    # NOTE(sileht): We currently assume we have only one parent
                    # commit in common. Since Git is a graph, in some case this
                    # graph can be more complicated.
                    # So, retrying with the whole git history for now
                    await git("fetch", "--unshallow")
                    await git("fetch", "--quiet", "origin", head_branch)
                    await git("fetch", "--quiet", "upstream", base_branch)
                    await git("rebase", f"upstream/{base_branch}")
                    await git("push", "--verbose", "origin", head_branch, "-f")
                    break
            else:
                raise

        expected_sha = (await git("log", "-1", "--format=%H")).strip()
        # NOTE(sileht): We store this for dismissal action
        await ctxt.redis.setex(f"branch-update-{expected_sha}", 60 * 60, expected_sha)
    except gitter.GitError as in_exception:  # pragma: no cover
        if in_exception.output == "":
            # SIGKILL...
            raise BranchUpdateNeedRetry()

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


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
)
async def update_with_api(ctxt: context.Context) -> None:
    try:
        await ctxt.client.put(
            f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/update-branch",
            api_version="lydian",  # type: ignore[call-arg]
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
    except (http.RequestError, http.HTTPStatusError) as e:
        status_code: typing.Optional[int] = None
        if isinstance(e, http.HTTPStatusError) and http.HTTPStatusError:
            status_code = e.response.status_code

        ctxt.log.info(
            "update branch failed",
            status_code=status_code,
            error=str(e),
        )
        raise BranchUpdateNeedRetry()


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(AuthenticationFailure),
)
async def rebase_with_git(
    ctxt: context.Context, user: typing.Optional[str] = None
) -> None:
    if user:
        token = ctxt.subscription.get_token_for(user)
        if token:
            creds = {user.lower(): token}
        else:
            raise BranchUpdateFailure(
                f"Unable to rebase: user `{user}` is unknown. "
                f"Please make sure `{user}` has logged in Mergify dashboard."
            )
    else:
        creds = ctxt.subscription.tokens

    for login, token in creds.items():
        try:
            return await _do_rebase(ctxt, token)
        except AuthenticationFailure as e:  # pragma: no cover
            ctxt.log.info(
                "authentification failure, will retry another token: %s",
                e,
                login=login,
            )

    ctxt.log.warning("unable to update branch: no tokens are valid")

    if ctxt.pull_from_fork and ctxt.pull["base"]["repo"]["private"]:
        raise BranchUpdateFailure(
            "Rebasing a branch for a forked private repository is not supported by GitHub"
        )

    raise AuthenticationFailure(
        f"No registered tokens allows Mergify to push to `{ctxt.pull['head']['label']}`"
    )
