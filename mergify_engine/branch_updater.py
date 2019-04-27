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

import daiquiri

from mergify_engine import config
from mergify_engine import sub_utils
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)

AUTHENTICATION_FAILURE_MESSAGES = [
    b"Invalid username or password",
    b"The requested URL returned error: 403",
]

class AuthentificationFailure(Exception):
    pass


def _do_update_branch(git, method, base_branch, head_branch):
    if method == "merge":
        git("merge", "--quiet", "upstream/%s" % base_branch, "-m",
            "Merge branch '%s' into '%s'" % (base_branch, head_branch))
        git("push", "--quiet", "origin", head_branch)
    elif method == "rebase":
        git("rebase", "upstream/%s" % base_branch)
        git("push", "--quiet", "origin", head_branch, "-f")
    else:
        raise RuntimeError("Invalid branch update method")


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

    head_repo = pull.g_pull.head.repo.full_name
    base_repo = pull.g_pull.base.repo.full_name

    head_branch = pull.g_pull.head.ref
    base_branch = pull.g_pull.base.ref
    git = utils.Gitter()
    try:
        git("init")
        git.configure()
        git.add_cred(token, "", head_repo)
        git.add_cred(token, "", base_repo)
        git("remote", "add", "origin",
            "https://%s/%s" % (config.GITHUB_DOMAIN, head_repo))
        git("remote", "add", "upstream",
            "https://%s/%s" % (config.GITHUB_DOMAIN, base_repo))

        depth = int(pull.g_pull.commits) + 1
        git("fetch", "--quiet", "--depth=%d" % depth, "origin", head_branch)
        git("checkout", "-q", "-b", head_branch, "origin/%s" % head_branch)

        out = git("log", "--format=%cI")
        last_commit_date = [d for d in out.decode("utf8").split("\n")
                            if d.strip()][-1]

        git("fetch", "--quiet", "upstream", base_branch,
            "--shallow-since='%s'" % last_commit_date)

        try:
            _do_update_branch(git, method, base_branch, head_branch)
        except subprocess.CalledProcessError as e:
            if b"unrelated histories" in e.output:
                LOG.debug("Complete history cloned", pull_request=pull)
                # NOTE(sileht): We currently assume we have only one parent
                # commit in common. Since Git is a graph, in some case this
                # graph can be more complicated.
                # So, retrying with the whole git history for now
                git("fetch", "--quiet", "origin", head_branch)
                git("fetch", "--quiet", "upstream", base_branch)
                _do_update_branch(git, method, base_branch, head_branch)
            else:
                raise

        return git("log", "-1", "--format=%H").decode().strip()
    except subprocess.CalledProcessError as e:  # pragma: no cover
        for message in AUTHENTICATION_FAILURE_MESSAGES:
            if message in e.output:
                raise AuthentificationFailure(e.output)
        else:
            LOG.error("update branch fail: %s", e.output, pull_request=pull,
                      exc_info=True)
    except Exception:  # pragma: no cover
        LOG.error("update branch fail", pull_request=pull, exc_info=True)
    finally:
        git.cleanup()


def update(pull, installation_id, method="merge"):
    redis = utils.get_redis_for_cache()

    subscription = sub_utils.get_subscription(redis, installation_id)

    if not subscription:
        LOG.error("subscription to update branch is missing")
        return

    for login, token in subscription["tokens"].items():
        try:
            return _do_update(pull, token, method)
        except AuthentificationFailure as e:
            LOG.error("Authentification failure: %s", e,
                      login=login, pull_request=pull)

    LOG.error("no tokens are valid to update branch : %s", e,
              pull_request=pull)
