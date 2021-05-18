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

from mergify_engine import context


def create_commit(sha=None):
    return {"sha": sha, "parents": []}


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
            cur["parents"].append(commits[-1])
        elif elem == "U":
            cur["parents"].append(create_commit("base"))
        elif elem == "O":
            cur["parents"].append(create_commit("outdated"))
        else:
            cur["parents"].append(create_commit(f"sha-{elem}"))
    commits.append(cur)
    return behind, commits


@pytest.mark.asyncio
async def test_pull_behind(commits_tree_generator, redis_cache):
    expected, commits = commits_tree_generator

    async def get_commits(*args, **kwargs):
        for c in commits:
            yield c

    async def item(*args, **kwargs):
        return {"commit": {"sha": "base"}}

    client = mock.Mock()
    client.items.return_value = get_commits()  # /pulls/X/commits

    client.item.return_value = item()  # /branch/#foo

    installation = context.Installation(123, "user", {}, client, redis_cache)
    repository = context.Repository(installation, {"name": "name", "id": 123456})
    ctxt = await context.Context.create(
        repository,
        {
            "number": 1,
            "mergeable_state": "clean",
            "state": "open",
            "merged": False,
            "merged_at": None,
            "merged_by": None,
            "base": {
                "ref": "#foo",
                "repo": {"name": "foobar", "private": False},
                "sha": "miaou",
                "user": {"login": "jd"},
            },
        },
        {},
    )

    assert expected == await ctxt.is_behind
