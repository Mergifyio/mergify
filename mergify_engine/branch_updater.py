# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
import subprocess
import uuid

import tenacity

from mergify_engine import config
from mergify_engine import utils
from mergify_engine.clients import http


class BranchUpdateFailure(Exception):
    def __init__(self, msg=""):
        error_code = "err-code: %s" % uuid.uuid4().hex[-5:].upper()
        self.message = msg + "\n" + error_code
        super(BranchUpdateFailure, self).__init__(self.message)


class BranchUpdateNeedRetry(Exception):
    pass


class AuthenticationFailure(Exception):
    pass


GIT_MESSAGE_TO_EXCEPTION = collections.OrderedDict(
    [
        (b"This repository was archived so it is read-only.", BranchUpdateFailure),
        (b"organization has enabled or enforced SAML SSO.", BranchUpdateFailure),
        (b"Invalid username or password", AuthenticationFailure),
        (b"Repository not found", AuthenticationFailure),
        (b"The requested URL returned error: 403", AuthenticationFailure),
        (b"Patch failed at", BranchUpdateFailure),
        (b"remote contains work that you do", BranchUpdateNeedRetry),
        (b"remote end hung up unexpectedly", BranchUpdateNeedRetry),
        (b"cannot lock ref 'refs/heads/", BranchUpdateNeedRetry),
        (b"Could not resolve host", BranchUpdateNeedRetry),
        (b"Operation timed out", BranchUpdateNeedRetry),
        (b"No such device or address", BranchUpdateNeedRetry),
        (b"Protected branch update failed", BranchUpdateFailure),
        (b"Couldn't find remote ref", BranchUpdateFailure),
    ]
)

GIT_MESSAGE_TO_UNSHALLOW = set([b"shallow update not allowed", b"unrelated histories"])


def _do_update_branch(git, method, base_branch, head_branch):
    if method == "merge":
        git(
            "merge",
            "--quiet",
            "upstream/%s" % base_branch,
            "-m",
            "Merge branch '%s' into '%s'" % (base_branch, head_branch),
        )
        git("push", "--quiet", "origin", head_branch)
    elif method == "rebase":
        git("rebase", "upstream/%s" % base_branch)
        git("push", "--quiet", "origin", head_branch, "-f")
    else:
        raise RuntimeError("Invalid branch update method")


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
)
def _do_update(ctxt, token, method="merge"):
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
    git = utils.Gitter(ctxt.log)
    try:
        git("init")
        git.configure()
        git.add_cred(token, "", head_repo)
        git.add_cred(token, "", base_repo)
        git("remote", "add", "origin", f"{config.GITHUB_URL}/{head_repo}")
        git("remote", "add", "upstream", f"{config.GITHUB_URL}/{base_repo}")

        depth = len(ctxt.commits) + 1
        git("fetch", "--quiet", "--depth=%d" % depth, "origin", head_branch)
        git("checkout", "-q", "-b", head_branch, "origin/%s" % head_branch)

        out = git("log", "--format=%cI")
        last_commit_date = [d for d in out.decode("utf8").split("\n") if d.strip()][-1]

        git(
            "fetch",
            "--quiet",
            "upstream",
            base_branch,
            "--shallow-since='%s'" % last_commit_date,
        )

        try:
            _do_update_branch(git, method, base_branch, head_branch)
        except subprocess.CalledProcessError as e:  # pragma: no cover
            for message in GIT_MESSAGE_TO_UNSHALLOW:
                if message in e.output:
                    ctxt.log.info("Complete history cloned")
                    # NOTE(sileht): We currently assume we have only one parent
                    # commit in common. Since Git is a graph, in some case this
                    # graph can be more complicated.
                    # So, retrying with the whole git history for now
                    git("fetch", "--unshallow")
                    git("fetch", "--quiet", "origin", head_branch)
                    git("fetch", "--quiet", "upstream", base_branch)
                    _do_update_branch(git, method, base_branch, head_branch)
                    break
            else:
                raise

        expected_sha = git("log", "-1", "--format=%H").decode().strip()
        # NOTE(sileht): We store this for dismissal action
        with utils.get_redis_for_cache() as redis:
            redis.setex("branch-update-%s" % expected_sha, 60 * 60, expected_sha)
    except subprocess.CalledProcessError as in_exception:  # pragma: no cover
        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in in_exception.output:
                raise out_exception(in_exception.output.decode())
        else:
            ctxt.log.error(
                "update branch failed: %s",
                in_exception.output.decode(),
                exc_info=True,
            )
            raise BranchUpdateFailure()

    except Exception:  # pragma: no cover
        ctxt.log.error("update branch failed", exc_info=True)
        raise BranchUpdateFailure()
    finally:
        git.cleanup()


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
)
def update_with_api(ctxt):
    try:
        ctxt.client.put(
            f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/update-branch",
            api_version="lydian",
            json={"expected_head_sha": ctxt.pull["head"]["sha"]},
        )
    except http.HTTPClientSideError as e:
        if e.status_code == 422:
            refreshed_pull = ctxt.client.item(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}"
            )
            if refreshed_pull["head"]["sha"] != ctxt.pull["head"]["sha"]:
                ctxt.log.info(
                    "branch updated in the meantime",
                    status=e.status_code,
                    error=e.message,
                )
                return
        ctxt.log.info(
            "update branch failed",
            status=e.status_code,
            error=e.message,
        )
        raise BranchUpdateFailure(e.message)
    except (http.RequestError, http.HTTPStatusError) as e:
        ctxt.log.info(
            "update branch failed",
            status=(e.response.status_code if e.response else None),
            error=str(e),
        )
        raise BranchUpdateNeedRetry()


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(AuthenticationFailure),
)
def update_with_git(ctxt, method="merge", user=None):
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
            return _do_update(ctxt, token, method)
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

    raise AuthenticationFailure("No valid OAuth tokens")
