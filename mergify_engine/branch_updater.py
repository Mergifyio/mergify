# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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


import subprocess
import uuid

import github
import tenacity

from mergify_engine import config
from mergify_engine import sub_utils
from mergify_engine import utils


UNRECOVERABLE_ERROR = ["head repository does not exist"]


class BranchUpdateFailure(Exception):
    def __init__(self, msg=""):
        error_code = "err-code: %s" % uuid.uuid4().hex[-5:].upper()
        self.message = msg + "\n" + error_code
        super(BranchUpdateFailure, self).__init__(self.message)


class BranchUpdateNeedRetry(Exception):
    pass


class AuthentificationFailure(Exception):
    pass


GIT_MESSAGE_TO_EXCEPTION = {
    b"Invalid username or password": AuthentificationFailure,
    b"Repository not found": AuthentificationFailure,
    b"The requested URL returned error: 403": AuthentificationFailure,
    b"Patch failed at": BranchUpdateFailure,
    b"remote contains work that you do": BranchUpdateNeedRetry,
    b"remote end hung up unexpectedly": BranchUpdateNeedRetry,
    b"cannot lock ref 'refs/heads/": BranchUpdateNeedRetry,
    b"Could not resolve host": BranchUpdateNeedRetry,
    b"Operation timed out": BranchUpdateNeedRetry,
    b"No such device or address": BranchUpdateNeedRetry,
    b"Protected branch update failed": BranchUpdateFailure,
    b"Couldn't find remote ref": BranchUpdateFailure,
}

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
def _do_update(pull, token, method="merge"):
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

    head_repo = pull.head_repo_owner_login + "/" + pull.head_repo_name
    base_repo = pull.base_repo_owner_login + "/" + pull.base_repo_name

    head_branch = pull.head_ref
    base_branch = pull.base_ref
    git = utils.Gitter()
    try:
        git("init")
        git.configure()
        git.add_cred(token, "", head_repo)
        git.add_cred(token, "", base_repo)
        git(
            "remote",
            "add",
            "origin",
            "https://%s/%s" % (config.GITHUB_DOMAIN, head_repo),
        )
        git(
            "remote",
            "add",
            "upstream",
            "https://%s/%s" % (config.GITHUB_DOMAIN, base_repo),
        )

        depth = len(pull.commits) + 1
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
                    pull.log.debug("Complete history cloned")
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
        redis = utils.get_redis_for_cache()
        redis.setex("branch-update-%s" % expected_sha, 60 * 60, expected_sha)
    except subprocess.CalledProcessError as in_exception:  # pragma: no cover
        for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if message in in_exception.output:
                raise out_exception(in_exception.output.decode())
        else:
            pull.log.error(
                "update branch failed: %s", in_exception.output.decode(), exc_info=True,
            )
            raise BranchUpdateFailure()

    except Exception:  # pragma: no cover
        pull.log.error("update branch failed", pull_request=pull, exc_info=True)
        raise BranchUpdateFailure()
    finally:
        git.cleanup()


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(BranchUpdateNeedRetry),
)
def update_with_api(pull):
    try:
        pull.g_pull._requester.requestJsonAndCheck(
            "PUT",
            pull.g_pull.url + "/update-branch",
            input={"expected_head_sha": pull.head_sha},
            headers={"Accept": "application/vnd.github.lydian-preview+json"},
        )
    except github.GithubException as e:
        if e.status == 422 and e.data["message"] not in UNRECOVERABLE_ERROR:
            pull.log.debug(
                "branch updated in the meantime",
                status=e.status,
                error=e.data["message"],
            )
            return
        elif e.status < 500:
            pull.log.debug(
                "update branch failed", status=e.status, error=e.data["message"],
            )
            raise BranchUpdateFailure(e.data["message"])
        else:
            pull.log.debug("update branch failed", status=e.status, error=str(e))
            raise BranchUpdateNeedRetry()


def update_with_git(pull, method="merge"):
    redis = utils.get_redis_for_cache()

    subscription = sub_utils.get_subscription(redis, pull.installation_id)

    for login, token in subscription["tokens"].items():
        try:
            return _do_update(pull, token, method)
        except AuthentificationFailure as e:  # pragma: no cover
            pull.log.debug(
                "authentification failure, will retry another token: %s",
                e,
                login=login,
            )

    pull.log.error("unable to update branch: no tokens are valid")
    raise BranchUpdateFailure("No oauth valid tokens")
