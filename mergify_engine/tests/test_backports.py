# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <julien@danjou.info>
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
import mock

from mergify_engine import backports
from mergify_engine import mergify_pull


def test_get_commits_to_cherry_pick_rebase():
    g_pull = mock.Mock()
    g_pull.merged = True

    c1 = mock.Mock()
    c1.sha = "c1f"
    c1.parents = []
    c2 = mock.Mock()
    c2.sha = "c2"
    c2.parents = [c1]

    def _get_commits():
        return [c1, c2]
    g_pull.get_commits = _get_commits

    pull = mergify_pull.MergifyPull(g_pull=g_pull)

    base_branch = mock.Mock()
    base_branch.sha = "base_branch"
    base_branch.parents = []
    rebased_c1 = mock.Mock()
    rebased_c1.sha = "rebased_c1"
    rebased_c1.parents = [base_branch]
    rebased_c2 = mock.Mock()
    rebased_c2.sha = "rebased_c2"
    rebased_c2.parents = [rebased_c1]

    assert (
        backports._get_commits_to_cherrypick(pull, rebased_c2) ==
        [rebased_c1, rebased_c2]
    )


def test_get_commits_to_cherry_pick_merge():
    g_pull = mock.Mock()
    g_pull.merged = True

    c1 = mock.Mock()
    c1.sha = "c1f"
    c1.parents = []
    c2 = mock.Mock()
    c2.sha = "c2"
    c2.parents = [c1]

    def _get_commits():
        return [c1, c2]
    g_pull.get_commits = _get_commits

    pull = mergify_pull.MergifyPull(g_pull=g_pull)

    base_branch = mock.Mock()
    base_branch.sha = "base_branch"
    base_branch.parents = []
    merge_commit = mock.Mock()
    merge_commit.sha = "merge_commit"
    merge_commit.parents = [base_branch, c2]

    assert backports._get_commits_to_cherrypick(pull, merge_commit) == [c1, c2]
