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
from unittest import mock

from mergify_engine import context
from mergify_engine import duplicate_pull


def fake_get_github_pulls_from_sha(url, api_version=None):
    pr = {
        "number": 6,
        "base": {
            "ref": "ref",
            "sha": "sha",
            "repo": {"full_name": "user/ref", "name": "name", "private": False},
        },
        "head": {
            "ref": "fork",
            "repo": {"full_name": "fork/other", "name": "other", "private": False},
        },
    }
    if url.endswith("commits/rebased_c1/pulls"):
        return [pr]
    elif url.endswith("commits/rebased_c2/pulls"):
        return [pr]
    else:
        return []


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
def test_get_commits_to_cherry_pick_rebase(commits):
    c1 = {"sha": "c1f", "parents": [], "commit": {"message": "foobar"}}
    c2 = {"sha": "c2", "parents": [c1], "commit": {"message": "foobar"}}
    commits.return_value = [c1, c2]

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"
    client.items.side_effect = fake_get_github_pulls_from_sha

    ctxt = context.Context(
        client,
        {
            "number": 6,
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "base": {
                "sha": "sha",
                "user": {"login": "user"},
                "ref": "ref",
                "repo": {"full_name": "user/ref", "name": "name", "private": False},
            },
            "head": {
                "sha": "sha",
                "ref": "fork",
                "repo": {"full_name": "fork/other", "name": "other", "private": False},
            },
            "user": {"login": "user"},
            "merged_by": None,
            "merged_at": None,
            "mergeable_state": "clean",
        },
        {},
    )

    base_branch = {"sha": "base_branch", "parents": []}
    rebased_c1 = {"sha": "rebased_c1", "parents": [base_branch]}
    rebased_c2 = {"sha": "rebased_c2", "parents": [rebased_c1]}

    assert duplicate_pull._get_commits_to_cherrypick(ctxt, rebased_c2) == [
        rebased_c1,
        rebased_c2,
    ]


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
def test_get_commits_to_cherry_pick_merge(commits):
    c1 = {"sha": "c1f", "parents": [], "commit": {"message": "foobar"}}
    c2 = {"sha": "c2", "parents": [c1], "commit": {"message": "foobar"}}
    commits.return_value = [c1, c2]

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"

    ctxt = context.Context(
        client,
        {
            "number": 6,
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "base": {
                "sha": "sha",
                "ref": "ref",
                "user": {"login": "user"},
                "repo": {"full_name": "user/ref", "name": "name", "private": False},
            },
            "head": {
                "sha": "sha",
                "ref": "fork",
                "repo": {"full_name": "fork/other", "name": "other", "private": False},
            },
            "user": {"login": "user"},
            "merged_at": None,
            "merged_by": None,
            "mergeable_state": "clean",
        },
        {},
    )

    base_branch = {"sha": "base_branch", "parents": []}
    merge_commit = {"sha": "merge_commit", "parents": [base_branch, c2]}

    assert duplicate_pull._get_commits_to_cherrypick(ctxt, merge_commit) == [c1, c2]
