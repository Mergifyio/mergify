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
from mergify_engine.clients import github


@pytest.mark.asyncio
async def test_user_permission_cache() -> None:
    class FakeClient(github.GithubInstallationClient):
        called: int

        def __init__(self, owner, repo):
            super().__init__(auth=None)
            self.owner = owner
            self.repo = repo
            self.called = 0

        def item(self, url, *args, **kwargs):
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

    owner = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(123),
            "login": github_types.GitHubLogin("jd"),
            "type": "User",
        }
    )

    repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test"),
            "private": False,
        }
    )

    def make_pr(
        repo: github_types.GitHubRepository, owner: github_types.GitHubAccount
    ) -> github_types.GitHubPullRequest:
        return github_types.GitHubPullRequest(
            {
                "id": github_types.GitHubPullRequestId(0),
                "maintainer_can_modify": False,
                "head": {
                    "user": owner,
                    "label": "",
                    "ref": github_types.GitHubRefType(""),
                    "sha": github_types.SHAType(""),
                    "repo": repo,
                },
                "user": owner,
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
                    "repo": repo,
                    "user": owner,
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

    sub = subscription.Subscription(0, False, "", {}, frozenset())
    client = FakeClient(owner["login"], repo["name"])
    c = context.Context(client, make_pr(repo, owner), sub)
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

    repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(1),
            "owner": owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test2"),
            "private": False,
        }
    )

    client = FakeClient(owner["login"], repo["name"])
    c = context.Context(client, make_pr(repo, owner), sub)
    assert client.called == 0
    assert await c.has_write_permission(user_2)
    assert client.called == 1
    assert await c.has_write_permission(user_2)
    assert client.called == 1
    assert not await c.has_write_permission(user_1)
    assert client.called == 2
    await context.Context.clear_user_permission_cache_for_repo(owner, repo)
    assert not await c.has_write_permission(user_1)
    assert client.called == 3
    assert not await c.has_write_permission(user_3)
    assert client.called == 4
    await context.Context.clear_user_permission_cache_for_org(owner)
    assert not await c.has_write_permission(user_3)
    assert client.called == 5
    assert await c.has_write_permission(user_2)
    assert client.called == 6
    assert await c.has_write_permission(user_2)
    assert client.called == 6
    await context.Context.clear_user_permission_cache_for_user(owner, repo, user_2)
    assert await c.has_write_permission(user_2)
    assert client.called == 7
