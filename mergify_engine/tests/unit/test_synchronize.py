# -*- encoding: utf-8 -*-
#
# Copyright © 2020—2021 Mergify SAS
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

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import subscription


@pytest.mark.asyncio
async def test_summary_synchronization_cache() -> None:
    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "archived": False,
            "url": "",
            "default_branch": github_types.GitHubRefType(""),
            "id": github_types.GitHubRepositoryIdType(456),
            "full_name": "user/ref",
            "name": github_types.GitHubRepositoryName("name"),
            "private": False,
            "owner": gh_owner,
        }
    )

    client = mock.MagicMock()
    client.auth.get_access_token.return_value = "<token>"

    sub = subscription.Subscription(0, False, "", {}, frozenset())
    installation = context.Installation(
        gh_owner["id"],
        gh_owner["login"],
        sub,
        client,
    )
    repository = context.Repository(installation, gh_repo["name"])
    ctxt = context.Context(
        repository,
        {
            "title": "",
            "id": github_types.GitHubPullRequestId(0),
            "maintainer_can_modify": False,
            "rebaseable": False,
            "draft": False,
            "merge_commit_sha": None,
            "labels": [],
            "number": github_types.GitHubPullRequestNumber(6),
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "base": {
                "label": "",
                "sha": github_types.SHAType("sha"),
                "user": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                },
                "ref": github_types.GitHubRefType("ref"),
                "label": "",
                "repo": gh_repo,
            },
            "head": {
                "label": "",
                "sha": github_types.SHAType("old-sha-one"),
                "ref": github_types.GitHubRefType("fork"),
                "user": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                },
                "repo": {
                    "archived": False,
                    "url": "",
                    "default_branch": github_types.GitHubRefType(""),
                    "id": github_types.GitHubRepositoryIdType(123),
                    "full_name": "fork/other",
                    "name": github_types.GitHubRepositoryName("other"),
                    "private": False,
                    "owner": {
                        "login": github_types.GitHubLogin("user"),
                        "id": github_types.GitHubAccountIdType(0),
                        "type": "User",
                    },
                },
            },
            "user": {
                "login": github_types.GitHubLogin("user"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
            },
            "merged_by": None,
            "merged_at": None,
            "mergeable_state": "clean",
        },
    )
    assert await ctxt.get_cached_last_summary_head_sha() is None
    await ctxt.set_summary_check(
        check_api.Result(check_api.Conclusion.SUCCESS, "foo", "bar")
    )

    assert await ctxt.get_cached_last_summary_head_sha() == "old-sha-one"
    ctxt.clear_cached_last_summary_head_sha()

    assert await ctxt.get_cached_last_summary_head_sha() is None
