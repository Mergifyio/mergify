# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

from unittest import mock

import pytest

from mergify_engine import mergify_pull


def create_commit(sha=None):
    return mock.Mock(sha=sha, parents=[], spec=["parents", "sha"])


@pytest.fixture(params=["U-A-B-C", "O-A-B-C", "O-A-BO-C", "O-A-BU-C", "O-A-B-CU"])
def commits_tree_generator(request):
    # NOTE(sileht):
    # tree direction: ->
    # U: mean HEAD of base branch
    # O: mean old commit of base branch
    commits = []
    cur = create_commit()
    tree = request.param
    behind = "U" not in tree

    while tree:
        elem = tree[0]
        tree = tree[1:]
        if elem == "-":
            commits.append(cur)
            cur = create_commit()
            cur.parents.append(commits[-1])
        elif elem == "U":
            cur.parents.append(create_commit("base"))
        elif elem == "O":
            cur.parents.append(create_commit("outdated"))
        else:
            cur.parents.append(create_commit("sha-%s" % elem))
    commits.append(cur)
    return behind, commits


def test_pull_behind(commits_tree_generator):
    expected, commits = commits_tree_generator
    with mock.patch(
        "mergify_engine.mergify_pull.MergifyPull.g_pull", return_value=mock.PropertyMock
    ) as g_pull:
        g_pull.base.repo.get_branch.return_value = mock.Mock(
            commit=mock.Mock(sha="base")
        )
        g_pull.get_commits.return_value = commits

        pull = mergify_pull.MergifyPull(
            mock.Mock(),
            data={"mergeable_state": "clean", "state": "open", "base": {"ref": "#foo"}},
        )

        assert expected == pull.is_behind
