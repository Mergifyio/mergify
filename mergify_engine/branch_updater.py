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

import logging

from mergify_engine import config
from mergify_engine import utils

LOG = logging.getLogger(__name__)


def update(pull, token, merge=True):
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

    head_repo_slug = pull.g_pull.head.repo.full_name
    base_repo_slug = pull.g_pull.base.repo.full_name

    head_branch = pull.g_pull.head.ref
    base_branch = pull.g_pull.base.ref

    git = utils.Gitter()
    try:
        git("clone", "--depth=%d" % (int(pull.g_pull.commits) + 1),
            "-b", pull.g_pull.head.ref,
            "https://%s@github.com/%s/" % (token, head_repo_slug),
            ".")
        git("remote", "add", "upstream",
            "https://%s@github.com/%s.git" % (token, base_repo_slug))
        git("config", "user.name", "%s-bot" % config.CONTEXT)
        git("config", "user.email", config.GIT_EMAIL)

        out = git("log", "--format=%cI")
        last_commit_date = [d for d in out.decode("utf8").split("\n")
                            if d.strip()][-1]

        git("fetch", "upstream", pull.g_pull.base.ref,
            "--shallow-since='%s'" % last_commit_date)
        if merge:
            git("merge", "upstream/%s" % base_branch, "-m",
                "Merge branch '%s' into '%s'" % (base_branch, head_branch))
        else:  # pragma: no cover
            # TODO(sileht): This will removes approvals, we need to add them
            # back
            git("rebase", "upstream/%s" % base_branch)
        git("push", "origin", head_branch)
    except Exception:
        LOG.error("%s: update branch fail", pull, exc_info=True)
        return False
    finally:
        git.cleanup()
    return True
