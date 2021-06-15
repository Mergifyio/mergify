# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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

from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import utils


async def fake_get_github_pulls_from_sha(url, api_version=None):
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
        yield pr
    elif url.endswith("commits/rebased_c2/pulls"):
        yield pr
    else:
        return


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
@pytest.mark.asyncio
async def test_get_commits_to_cherry_pick_rebase(
    commits: mock.PropertyMock,
    redis_cache: utils.RedisCache,
) -> None:
    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "user/name",
            "name": github_types.GitHubRepositoryName("name"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )

    c1 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("c1f"),
            "parents": [],
            "commit": {"message": "foobar"},
        }
    )
    c2 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("c2"),
            "parents": [c1],
            "commit": {"message": "foobar"},
        }
    )
    commits.return_value = [c1, c2]

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"
    client.items.side_effect = fake_get_github_pulls_from_sha

    installation = context.Installation(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user"),
        subscription.Subscription(redis_cache, 0, False, "", frozenset()),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)
    ctxt = await context.Context.create(
        repository,
        {
            "locked": False,
            "assignees": [],
            "requested_reviewers": [],
            "requested_teams": [],
            "milestone": None,
            "labels": [],
            "draft": False,
            "merge_commit_sha": github_types.SHAType(""),
            "title": "",
            "body": "",
            "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "closed_at": None,
            "commits": 1,
            "rebaseable": False,
            "maintainer_can_modify": False,
            "id": github_types.GitHubPullRequestId(0),
            "number": github_types.GitHubPullRequestNumber(6),
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "changed_files": 1,
            "base": {
                "label": "",
                "sha": github_types.SHAType("sha"),
                "user": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                    "avatar_url": "",
                },
                "ref": github_types.GitHubRefType("ref"),
                "repo": {
                    "full_name": "user/ref",
                    "name": github_types.GitHubRepositoryName("name"),
                    "private": False,
                    "id": github_types.GitHubRepositoryIdType(0),
                    "owner": {
                        "login": github_types.GitHubLogin("user"),
                        "id": github_types.GitHubAccountIdType(0),
                        "type": "User",
                        "avatar_url": "",
                    },
                    "archived": False,
                    "url": "",
                    "html_url": "",
                    "default_branch": github_types.GitHubRefType(""),
                },
            },
            "head": {
                "label": "",
                "sha": github_types.SHAType("sha"),
                "ref": github_types.GitHubRefType("fork"),
                "user": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                    "avatar_url": "",
                },
                "repo": {
                    "full_name": "fork/other",
                    "name": github_types.GitHubRepositoryName("other"),
                    "private": False,
                    "archived": False,
                    "url": "",
                    "html_url": "",
                    "default_branch": github_types.GitHubRefType(""),
                    "id": github_types.GitHubRepositoryIdType(0),
                    "owner": {
                        "login": github_types.GitHubLogin("user"),
                        "id": github_types.GitHubAccountIdType(0),
                        "type": "User",
                        "avatar_url": "",
                    },
                },
            },
            "user": {
                "login": github_types.GitHubLogin("user"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
                "avatar_url": "",
            },
            "merged_by": None,
            "merged_at": None,
            "mergeable_state": "clean",
        },
    )

    base_branch = github_types.GitHubBranchCommitParent(
        {"sha": github_types.SHAType("base_branch")}
    )
    rebased_c1 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("rebased_c1"),
            "parents": [base_branch],
            "commit": {"message": "hello c1"},
        }
    )
    rebased_c2 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("rebased_c2"),
            "parents": [rebased_c1],
            "commit": {"message": "hello c2"},
        }
    )

    async def fake_get_github_commit_from_sha(url, api_version=None):
        if url.endswith("/commits/rebased_c1"):
            return rebased_c1
        if url.endswith("/commits/rebased_c2"):
            return rebased_c2
        raise RuntimeError(f"Unknown URL {url}")

    client.item.side_effect = fake_get_github_commit_from_sha

    assert await duplicate_pull._get_commits_to_cherrypick(ctxt, rebased_c2) == [
        rebased_c1,
        rebased_c2,
    ]


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
@pytest.mark.asyncio
async def test_get_commits_to_cherry_pick_merge(
    commits: mock.PropertyMock,
    redis_cache: utils.RedisCache,
) -> None:
    c1 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("c1f"),
            "parents": [],
            "commit": {"message": "foobar"},
        }
    )
    c2 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("c2"),
            "parents": [c1],
            "commit": {"message": "foobar"},
        }
    )

    async def fake_commits() -> typing.List[github_types.GitHubBranchCommit]:
        return [c1, c2]

    commits.return_value = fake_commits()

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"

    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )
    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "user/name",
            "name": github_types.GitHubRepositoryName("name"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )

    installation = context.Installation(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user"),
        subscription.Subscription(redis_cache, 0, False, "", frozenset()),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)
    ctxt = await context.Context.create(
        repository,
        {
            "locked": False,
            "assignees": [],
            "requested_reviewers": [],
            "requested_teams": [],
            "milestone": None,
            "number": github_types.GitHubPullRequestNumber(6),
            "commits": 1,
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "id": github_types.GitHubPullRequestId(0),
            "maintainer_can_modify": True,
            "labels": [],
            "rebaseable": True,
            "draft": True,
            "merge_commit_sha": None,
            "title": "foobar",
            "body": "",
            "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "closed_at": None,
            "changed_files": 1,
            "base": {
                "label": "user:ref",
                "sha": github_types.SHAType("sha"),
                "ref": github_types.GitHubRefType("ref"),
                "user": gh_owner,
                "repo": github_types.GitHubRepository(
                    {
                        "full_name": "user/ref",
                        "name": github_types.GitHubRepositoryName("name"),
                        "private": False,
                        "id": github_types.GitHubRepositoryIdType(0),
                        "owner": gh_owner,
                        "archived": False,
                        "url": "",
                        "html_url": "",
                        "default_branch": github_types.GitHubRefType("ref"),
                    }
                ),
            },
            "head": {
                "label": "user:ref",
                "sha": github_types.SHAType("sha"),
                "user": gh_owner,
                "ref": github_types.GitHubRefType("fork"),
                "repo": github_types.GitHubRepository(
                    {
                        "full_name": "fork/other",
                        "name": github_types.GitHubRepositoryName("name"),
                        "private": False,
                        "id": github_types.GitHubRepositoryIdType(0),
                        "owner": gh_owner,
                        "archived": False,
                        "url": "",
                        "html_url": "",
                        "default_branch": github_types.GitHubRefType("ref"),
                    }
                ),
            },
            "user": gh_owner,
            "merged_at": None,
            "merged_by": None,
            "mergeable_state": "clean",
        },
    )

    base_branch = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("base_branch"),
            "parents": [],
            "commit": {"message": "foobar"},
        }
    )
    merge_commit = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("merge_commit"),
            "parents": [base_branch, c2],
            "commit": {"message": "foobar"},
        }
    )

    assert await duplicate_pull._get_commits_to_cherrypick(ctxt, merge_commit) == [
        c1,
        c2,
    ]
