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
import sys

import github

from mergify_engine import config
from mergify_engine import utils

LOG = logging.getLogger(__name__)


def update_branch(self, token, merge=True):
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

    git = utils.Gitter()
    try:
        git("clone", "--depth=%d" % (int(self.commits) + 1),
            "-b", self.head.ref,
            "https://%s@github.com/%s/" % (token, self.head.repo.full_name),
            ".")
        git("remote", "add", "upstream",
            "https://%s@github.com/%s.git" % (token, self.base.repo.full_name))
        git("config", "user.name", "%s-bot" % config.CONTEXT)
        git("config", "user.email", config.GIT_EMAIL)

        out = git("log", "--pretty='format:%cI'")
        last_commit_date = out.decode("utf8").split("\n")[-1]

        git("fetch", "upstream", self.base.ref,
            "--shallow-since='%s'" % last_commit_date)
        if merge:
            git("merge", "upstream/%s" % self.base.ref, "-m",
                "Merge branch '%s' into '%s'" % (self.base.ref, self.head.ref))
        else:
            # TODO(sileht): This will removes approvals, we need to add them
            # back
            git("rebase", "upstream/%s" % self.base.ref)
        git("push", "origin", self.head.ref)
    except Exception:
        LOG.error("%s: update branch fail", self.pretty(), exc_info=True)
        return False
    finally:
        git.cleanup()
    return True
