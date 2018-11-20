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

import daiquiri

from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


def update(pull, token):
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
        git("remote", "add", "origin", "https://github.com/%s" % head_repo)
        git("remote", "add", "upstream", "https://github.com/%s" % base_repo)

        depth = int(pull.g_pull.commits) + 1
        git("fetch", "--quiet", "--depth=%d" % depth, "origin", head_branch)
        git("checkout", "-q", "-b", head_branch, "origin/%s" % head_branch)

        out = git("log", "--format=%cI")
        last_commit_date = [d for d in out.decode("utf8").split("\n")
                            if d.strip()][-1]

        git("fetch", "--quiet", "upstream", pull.g_pull.base.ref,
            "--shallow-since='%s'" % last_commit_date)
        git("merge", "--quiet", "upstream/%s" % base_branch, "-m",
            "Merge branch '%s' into '%s'" % (base_branch, head_branch))
        commit_id = git("log", "-1", "--format=%H").decode()
        git("push", "--quiet", "origin", head_branch)
        return commit_id
    except Exception:  # pragma: no cover
        LOG.error("update branch fail", pull_request=pull, exc_info=True)
    finally:
        git.cleanup()
