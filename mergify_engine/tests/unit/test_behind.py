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

import typing
from unittest import mock

import pytest

from mergify_engine import github_types
from mergify_engine.tests.unit import conftest


def create_commit(sha: github_types.SHAType) -> github_types.GitHubBranchCommit:
    return github_types.GitHubBranchCommit(
        {
            "sha": sha,
            "parents": [],
            "commit": {"message": "", "verification": {"verified": False}},
        }
    )


@pytest.fixture(
    params=[
        "U-A-B-C",
        "O-A-B-C",
        "O-A-BO-C",
        "O-A-BU-C",
        "O-A-B-CU",
        "O-A-PB-CU",
        "P-A-B-CU",
        "P-A-B-C",
        "O-AP-BP-C",
        "O-AP-B-CP",
    ]
)
def commits_tree_generator(request):
    # NOTE(sileht):
    # tree direction: ->
    # U: mean HEAD of base branch
    # O: mean old commit of base branch
    # P: mean another unknown branch
    commits = []
    cur = create_commit("whatever")
    tree = request.param
    behind = "U" not in tree

    while tree:
        elem = tree[0]
        tree = tree[1:]
        if elem == "-":
            commits.append(cur)
            cur = create_commit("whatever")
            cur["parents"].append(commits[-1])
        elif elem == "U":
            cur["parents"].append(create_commit("base"))
        elif elem == "O":
            cur["parents"].append(create_commit("outdated"))
        elif elem == "P":
            cur["parents"].append(create_commit("random-branch"))
        else:
            cur["parents"].append(create_commit(f"sha-{elem}"))
    commits.append(cur)
    return behind, commits


async def test_pull_behind(
    commits_tree_generator: typing.Any, context_getter: conftest.ContextGetterFixture
) -> None:
    expected, commits = commits_tree_generator

    async def get_commits(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        # /pulls/X/commits
        for c in commits:
            yield c

    async def item(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        # /branch/#foo
        return {"commit": {"sha": "base"}}

    async def get_compare(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        return github_types.GitHubCompareCommits({"behind_by": 0 if expected else 100})

    client = mock.Mock()
    client.items.return_value = get_commits()
    client.item.side_effect = [item(), get_compare()]

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client
    assert expected == await ctxt.is_behind
