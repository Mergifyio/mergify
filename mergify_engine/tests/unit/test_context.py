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
import pytest

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github


@pytest.mark.asyncio
async def test_user_permission_cache(redis_cache: utils.RedisCache) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner, repo):
            super().__init__(auth=None)
            self.owner = owner
            self.repo = repo
            self.called = 0

        async def item(self, url, *args, **kwargs):
            self.called += 1
            if self.repo == "test":
                if (
                    url
                    == f"/repos/{self.owner}/{self.repo}/collaborators/foo/permission"
                ):
                    return {"permission": "admin"}
                elif url.startswith(f"/repos/{self.owner}/{self.repo}/collaborators/"):
                    return {"permission": "loser"}
            elif self.repo == "test2":
                if (
                    url
                    == f"/repos/{self.owner}/{self.repo}/collaborators/bar/permission"
                ):
                    return {"permission": "admin"}
                elif url.startswith(f"/repos/{self.owner}/{self.repo}/collaborators/"):
                    return {"permission": "loser"}
            raise ValueError(f"Unknown test URL `{url}` for repo {self.repo}")

    gh_owner = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(123),
            "login": github_types.GitHubLogin("jd"),
            "type": "User",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test"),
            "private": False,
        }
    )

    def make_pr(
        gh_repo: github_types.GitHubRepository, gh_owner: github_types.GitHubAccount
    ) -> github_types.GitHubPullRequest:
        return github_types.GitHubPullRequest(
            {
                "title": "",
                "id": github_types.GitHubPullRequestId(0),
                "maintainer_can_modify": False,
                "head": {
                    "user": gh_owner,
                    "label": "",
                    "ref": github_types.GitHubRefType(""),
                    "sha": github_types.SHAType(""),
                    "repo": gh_repo,
                },
                "user": gh_owner,
                "number": github_types.GitHubPullRequestNumber(0),
                "rebaseable": False,
                "draft": False,
                "merge_commit_sha": None,
                "html_url": "",
                "state": "closed",
                "mergeable_state": "unknown",
                "merged_by": None,
                "merged": False,
                "merged_at": None,
                "labels": [],
                "base": {
                    "ref": github_types.GitHubRefType("main"),
                    "sha": github_types.SHAType(""),
                    "label": "",
                    "repo": gh_repo,
                    "user": gh_owner,
                },
            }
        )

    user_1 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(1),
            "login": github_types.GitHubLogin("foo"),
            "type": "User",
        }
    )
    user_2 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(2),
            "login": github_types.GitHubLogin("bar"),
            "type": "User",
        }
    )
    user_3 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(3),
            "login": github_types.GitHubLogin("baz"),
            "type": "User",
        }
    )

    sub = subscription.Subscription(redis_cache, 0, False, "", {}, frozenset())
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo["name"])
    c = await context.Context.create(repository, make_pr(gh_repo, gh_owner))
    assert client.called == 0
    assert await c.has_write_permission(user_1)
    assert client.called == 1
    assert await c.has_write_permission(user_1)
    assert client.called == 1
    assert not await c.has_write_permission(user_2)
    assert client.called == 2
    assert not await c.has_write_permission(user_2)
    assert client.called == 2
    assert not await c.has_write_permission(user_3)
    assert client.called == 3

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(1),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test2"),
            "private": False,
        }
    )

    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo["name"])
    c = await context.Context.create(repository, make_pr(gh_repo, gh_owner))
    assert client.called == 0
    assert await c.has_write_permission(user_2)
    assert client.called == 1
    assert await c.has_write_permission(user_2)
    assert client.called == 1
    assert not await c.has_write_permission(user_1)
    assert client.called == 2
    await context.Context.clear_user_permission_cache_for_repo(
        redis_cache, gh_owner, gh_repo
    )
    assert not await c.has_write_permission(user_1)
    assert client.called == 3
    assert not await c.has_write_permission(user_3)
    assert client.called == 4
    await context.Context.clear_user_permission_cache_for_org(redis_cache, gh_owner)
    assert not await c.has_write_permission(user_3)
    assert client.called == 5
    assert await c.has_write_permission(user_2)
    assert client.called == 6
    assert await c.has_write_permission(user_2)
    assert client.called == 6
    await context.Context.clear_user_permission_cache_for_user(
        redis_cache, gh_owner, gh_repo, user_2
    )
    assert await c.has_write_permission(user_2)
    assert client.called == 7
