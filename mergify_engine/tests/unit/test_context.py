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
import typing
from unittest import mock

import pytest

from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http


@pytest.mark.asyncio
async def test_user_permission_cache(redis_cache: utils.RedisCache) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str, repo: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
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
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test"),
            "private": False,
        }
    )

    user_1 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(1),
            "login": github_types.GitHubLogin("foo"),
            "type": "User",
            "avatar_url": "",
        }
    )
    user_2 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(2),
            "login": github_types.GitHubLogin("bar"),
            "type": "User",
            "avatar_url": "",
        }
    )
    user_3 = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(3),
            "login": github_types.GitHubLogin("baz"),
            "type": "User",
            "avatar_url": "",
        }
    )

    sub = subscription.Subscription(redis_cache, 0, False, "", frozenset())
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert await repository.has_write_permission(user_1)
    assert client.called == 1
    assert await repository.has_write_permission(user_1)
    assert client.called == 1
    assert not await repository.has_write_permission(user_2)
    assert client.called == 2
    assert not await repository.has_write_permission(user_2)
    assert client.called == 2
    assert not await repository.has_write_permission(user_3)
    assert client.called == 3

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(1),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test2"),
            "private": False,
        }
    )

    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert await repository.has_write_permission(user_2)
    assert client.called == 1
    assert await repository.has_write_permission(user_2)
    assert client.called == 1
    assert not await repository.has_write_permission(user_1)
    assert client.called == 2
    await context.Repository.clear_user_permission_cache_for_repo(
        redis_cache, gh_owner, gh_repo
    )
    assert not await repository.has_write_permission(user_1)
    assert client.called == 3
    assert not await repository.has_write_permission(user_3)
    assert client.called == 4
    await context.Repository.clear_user_permission_cache_for_org(redis_cache, gh_owner)
    assert not await repository.has_write_permission(user_3)
    assert client.called == 5
    assert await repository.has_write_permission(user_2)
    assert client.called == 6
    assert await repository.has_write_permission(user_2)
    assert client.called == 6
    await context.Repository.clear_user_permission_cache_for_user(
        redis_cache, gh_owner, gh_repo, user_2
    )
    assert await repository.has_write_permission(user_2)
    assert client.called == 7


@pytest.mark.asyncio
async def test_team_members_cache(redis_cache: utils.RedisCache) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
            self.owner = owner
            self.called = 0

        async def items(self, url, *args, **kwargs):
            self.called += 1
            if url == f"/orgs/{self.owner}/teams/team1/members":
                yield {"login": "member1"}
                yield {"login": "member2"}
            elif url == f"/orgs/{self.owner}/teams/team2/members":
                yield {"login": "member3"}
                yield {"login": "member4"}
            elif url == f"/orgs/{self.owner}/teams/team3/members":
                return
            else:
                raise ValueError(f"Unknown test URL `{url}` for repo {self.repo}")

    gh_owner = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(123),
            "login": github_types.GitHubLogin("jd"),
            "type": "User",
            "avatar_url": "",
        }
    )

    team_slug1 = github_types.GitHubTeamSlug("team1")
    team_slug2 = github_types.GitHubTeamSlug("team2")
    team_slug3 = github_types.GitHubTeamSlug("team3")

    sub = subscription.Subscription(redis_cache, 0, False, "", frozenset())
    client = FakeClient(gh_owner["login"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    assert client.called == 0
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 1
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 1
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 2
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 2
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 3
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 3
    await installation.clear_team_members_cache_for_team(
        redis_cache, gh_owner, github_types.GitHubTeamSlug(team_slug2)
    )
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 4
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 4
    await installation.clear_team_members_cache_for_org(redis_cache, gh_owner)
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 5
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 5
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 6
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 6
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 7
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 7


@pytest.mark.asyncio
async def test_team_permission_cache(redis_cache: utils.RedisCache) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str, repo: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
            self.owner = owner
            self.repo = repo
            self.called = 0

        async def get(self, url: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:  # type: ignore[override]
            self.called += 1
            if (
                url
                == f"/orgs/{self.owner}/teams/team-ok/repos/{self.owner}/{self.repo}"
            ):
                return {}
            elif (
                url
                == f"/orgs/{self.owner}/teams/team-nok/repos/{self.owner}/{self.repo}"
            ):
                raise http.HTTPNotFound(
                    message="Not found", request=mock.ANY, response=mock.ANY
                )
            elif (
                url
                == f"/orgs/{self.owner}/teams/team-also-nok/repos/{self.owner}/{self.repo}"
            ):
                raise http.HTTPNotFound(
                    message="Not found", request=mock.ANY, response=mock.ANY
                )

            raise ValueError(f"Unknown test URL `{url}`")

    gh_owner = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(123),
            "login": github_types.GitHubLogin("jd"),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test"),
            "private": False,
        }
    )

    team_slug1 = github_types.GitHubTeamSlug("team-ok")
    team_slug2 = github_types.GitHubTeamSlug("team-nok")
    team_slug3 = github_types.GitHubTeamSlug("team-also-nok")

    sub = subscription.Subscription(redis_cache, 0, False, "", frozenset())
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 1
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 1
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 2
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 2
    assert not await repository.team_has_read_permission(team_slug3)
    assert client.called == 3

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(1),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test2"),
            "private": False,
        }
    )

    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(
        gh_owner["id"], gh_owner["login"], sub, client, redis_cache
    )
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 1
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 1
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 2
    await context.Repository.clear_team_permission_cache_for_repo(
        redis_cache, gh_owner, gh_repo
    )
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 3
    assert not await repository.team_has_read_permission(team_slug3)
    assert client.called == 4
    await context.Repository.clear_team_permission_cache_for_org(redis_cache, gh_owner)
    assert not await repository.team_has_read_permission(team_slug3)
    assert client.called == 5
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 6
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 6
    await context.Repository.clear_team_permission_cache_for_team(
        redis_cache, gh_owner, team_slug2
    )
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 7


@pytest.mark.asyncio
async def test_context_depends_on():
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
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "id": github_types.GitHubRepositoryIdType(456),
            "full_name": "user/repo",
            "name": github_types.GitHubRepositoryName("repo"),
            "private": False,
            "owner": gh_owner,
        }
    )

    pull = github_types.GitHubPullRequest(
        {
            "locked": False,
            "assignees": [],
            "requested_reviewers": [],
            "requested_teams": [],
            "milestone": None,
            "title": "",
            "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
            "closed_at": None,
            "id": github_types.GitHubPullRequestId(0),
            "maintainer_can_modify": False,
            "rebaseable": False,
            "draft": False,
            "merge_commit_sha": None,
            "labels": [],
            "number": github_types.GitHubPullRequestNumber(6),
            "commits": 1,
            "merged": True,
            "state": "closed",
            "changed_files": 1,
            "html_url": "<html_url>",
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
                    "avatar_url": "",
                },
                "repo": {
                    "archived": False,
                    "url": "",
                    "html_url": "",
                    "default_branch": github_types.GitHubRefType(""),
                    "id": github_types.GitHubRepositoryIdType(123),
                    "full_name": "fork/other",
                    "name": github_types.GitHubRepositoryName("other"),
                    "private": False,
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
            "body": f"""header

Depends-On: #123
depends-on: {config.GITHUB_URL}/foo/bar/pull/999
depends-on: {config.GITHUB_URL}/foo/bar/999
depends-on: azertyuiopqsdfghjklmwxcvbn
depends-on: https://somewhereelse.com/foo/bar/999
Depends-oN: {config.GITHUB_URL}/user/repo/pull/456
Depends-oN: {config.GITHUB_URL}/user/repo/pull/789
 DEPENDS-ON: #42
Depends-On:  #48
Depends-On:  #999 with crap
DePeNdS-oN: {config.GITHUB_URL}/user/repo/pull/999 with crap

footer
""",
        },
    )

    ctxt = await context.Context.create(mock.Mock(), pull)
    assert ctxt.get_depends_on() == {123, 456, 789, 42, 48}
