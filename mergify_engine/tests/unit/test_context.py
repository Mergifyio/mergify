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
from mergify_engine import redis_utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription


async def test_user_permission_cache(redis_links: redis_utils.RedisLinks) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str, repo: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
            self.owner = owner
            self.repo = repo
            self.called = 0

        async def item(
            self, url: str, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Optional[typing.Dict[str, str]]:
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
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    sub = subscription.Subscription(
        redis_links.cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
    )
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(installation_json, sub, client, redis_links)
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert await repository.has_write_permission(user_1)
    assert client.called == 1
    assert await repository.has_write_permission(user_1)
    assert client.called == 1
    assert not await repository.has_write_permission(user_2)
    assert client.called == 2
    # From local cache
    assert not await repository.has_write_permission(user_2)
    assert client.called == 2
    # From redis
    repository._caches.user_permissions.clear()
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
    installation = context.Installation(installation_json, sub, client, redis_links)
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert await repository.has_write_permission(user_2)
    assert client.called == 1
    # From local cache
    assert await repository.has_write_permission(user_2)
    assert client.called == 1
    # From redis
    repository._caches.user_permissions.clear()
    assert await repository.has_write_permission(user_2)
    assert client.called == 1

    assert not await repository.has_write_permission(user_1)
    assert client.called == 2
    await context.Repository.clear_user_permission_cache_for_repo(
        redis_links.user_permissions_cache, gh_owner, gh_repo
    )
    repository._caches.user_permissions.clear()
    assert not await repository.has_write_permission(user_1)
    assert client.called == 3
    assert not await repository.has_write_permission(user_3)
    assert client.called == 4
    await context.Repository.clear_user_permission_cache_for_org(
        redis_links.user_permissions_cache, gh_owner
    )
    repository._caches.user_permissions.clear()
    assert not await repository.has_write_permission(user_3)
    assert client.called == 5
    assert await repository.has_write_permission(user_2)
    assert client.called == 6
    # From local cache
    assert await repository.has_write_permission(user_2)
    assert client.called == 6
    # From redis
    repository._caches.user_permissions.clear()
    assert await repository.has_write_permission(user_2)
    assert client.called == 6
    await context.Repository.clear_user_permission_cache_for_user(
        redis_links.user_permissions_cache, gh_owner, gh_repo, user_2
    )
    repository._caches.user_permissions.clear()
    assert await repository.has_write_permission(user_2)
    assert client.called == 7


async def test_team_members_cache(redis_links: redis_utils.RedisLinks) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str, repo: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
            self.owner = owner
            self.repo = repo
            self.called = 0

        async def items(
            self, url: str, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Optional[typing.AsyncGenerator[typing.Dict[str, str], None]]:
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
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    team_slug1 = github_types.GitHubTeamSlug("team1")
    team_slug2 = github_types.GitHubTeamSlug("team2")
    team_slug3 = github_types.GitHubTeamSlug("team3")

    sub = subscription.Subscription(
        redis_links.cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
    )
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(installation_json, sub, client, redis_links)
    assert client.called == 0
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 1
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 1
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 2
    # From local cache
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 2
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 2

    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 3
    # From local cache
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 3
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 3

    await installation.clear_team_members_cache_for_team(
        redis_links.team_members_cache,
        gh_owner,
        github_types.GitHubTeamSlug(team_slug2),
    )
    installation._caches.team_members.clear()

    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 4
    # From local cache
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 4
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 4

    await installation.clear_team_members_cache_for_org(
        redis_links.team_members_cache, gh_owner
    )
    installation._caches.team_members.clear()

    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 5
    # From local cache
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 5
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug1)) == ["member1", "member2"]
    assert client.called == 5

    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 6
    # From local cache
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 6
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug2)) == ["member3", "member4"]
    assert client.called == 6
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 7
    # From local cache
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 7
    # From redis
    installation._caches.team_members.clear()
    assert (await installation.get_team_members(team_slug3)) == []
    assert client.called == 7


async def test_team_permission_cache(redis_links: redis_utils.RedisLinks) -> None:
    class FakeClient(github.AsyncGithubInstallationClient):
        called: int

        def __init__(self, owner: str, repo: str) -> None:
            super().__init__(auth=None)  # type: ignore[arg-type]
            self.owner = owner
            self.repo = repo
            self.called = 0

        async def get(  # type: ignore[override]
            self, url: str, *args: typing.Any, **kwargs: typing.Any
        ) -> typing.Optional[typing.Dict[typing.Any, typing.Any]]:
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
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    team_slug1 = github_types.GitHubTeamSlug("team-ok")
    team_slug2 = github_types.GitHubTeamSlug("team-nok")
    team_slug3 = github_types.GitHubTeamSlug("team-also-nok")

    sub = subscription.Subscription(
        redis_links.cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
    )
    client = FakeClient(gh_owner["login"], gh_repo["name"])
    installation = context.Installation(installation_json, sub, client, redis_links)
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
    installation = context.Installation(installation_json, sub, client, redis_links)
    repository = context.Repository(installation, gh_repo)
    assert client.called == 0
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 1
    # From local cache
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 1
    # From redis
    repository._caches.team_has_read_permission.clear()
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 1
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 2
    await context.Repository.clear_team_permission_cache_for_repo(
        redis_links.team_permissions_cache, gh_owner, gh_repo
    )
    repository._caches.team_has_read_permission.clear()
    assert await repository.team_has_read_permission(team_slug1)
    assert client.called == 3
    assert not await repository.team_has_read_permission(team_slug3)
    assert client.called == 4
    await context.Repository.clear_team_permission_cache_for_org(
        redis_links.team_permissions_cache, gh_owner
    )
    repository._caches.team_has_read_permission.clear()
    assert not await repository.team_has_read_permission(team_slug3)
    assert client.called == 5
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 6
    # From local cache
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 6
    # From redis
    repository._caches.team_has_read_permission.clear()
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 6
    repository._caches.team_has_read_permission.clear()
    await context.Repository.clear_team_permission_cache_for_team(
        redis_links.team_permissions_cache, gh_owner, team_slug2
    )
    repository._caches.team_has_read_permission.clear()
    assert not await repository.team_has_read_permission(team_slug2)
    assert client.called == 7


@pytest.fixture
def a_pull_request() -> github_types.GitHubPullRequest:
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

    return github_types.GitHubPullRequest(
        {
            "node_id": "42",
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
                "label": github_types.GitHubBaseBranchLabel(""),
                "sha": github_types.SHAType("sha"),
                "user": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                    "avatar_url": "",
                },
                "ref": github_types.GitHubRefType("ref"),
                "repo": gh_repo,
            },
            "head": {
                "label": github_types.GitHubHeadBranchLabel(""),
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
            "mergeable": True,
            "body": None,
        }
    )


async def test_length_optimisation(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request["commits"] = 10
    a_pull_request["changed_files"] = 5
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await getattr(ctxt.pull_request, "#commits") == 10
    assert await getattr(ctxt.pull_request, "#files") == 5


async def test_context_depends_on(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request[
        "body"
    ] = f"""header

Depends-On: #123
depends-on: {config.GITHUB_URL}/foo/bar/pull/999
depends-on: {config.GITHUB_URL}/foo/bar/999
depends-on: azertyuiopqsdfghjklmwxcvbn
depends-on: https://somewhereelse.com/foo/bar/999
Depends-oN: {config.GITHUB_URL}/user/repo/pull/456
Depends-oN: {config.GITHUB_URL}/user/repo/pull/457\r
Depends-oN: {config.GITHUB_URL}/user/repo/pull/789
 DEPENDS-ON: #42
Depends-On:  #48
Depends-On:  #50\r
Depends-On:  #999 with crap
DePeNdS-oN: {config.GITHUB_URL}/user/repo/pull/999 with crap

footer
"""

    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert ctxt.get_depends_on() == [42, 48, 50, 123, 456, 457, 789]


async def test_context_body_null(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request["body"] = None
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await ctxt._get_consolidated_data("body") == ""


async def test_context_body_html(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request["title"] = "chore(deps-dev): update flake8 requirement from <4 to <5"
    a_pull_request[
        "body"
    ] = """
Updates the requirements on [flake8](https://github.com/pycqa/flake8) to permit the latest version.
<details>
<summary>Commits</summary>
<ul>
<li><a href="https://github.com/PyCQA/flake8/commit/82b698e09996cdde5d473e234681d8380810d7a2"><code>82b698e</code></a> Release 4.0.1</li>
<li><a href="https://github.com/PyCQA/flake8/commit/0fac346d8437d205e508643253c7a7d5fdf5dee7"><code>0fac346</code></a> Merge pull request <a href="https://github-redirect.dependabot.com/pycqa/flake8/issues/1410">#1410</a> from PyCQA/parallel-syntax-error</li>
<li><a href="https://github.com/PyCQA/flake8/commit/aa54693c9ec03368c6e592efff4dd4757dd72a47"><code>aa54693</code></a> fix parallel execution collecting a SyntaxError</li>
<li><a href="https://github.com/PyCQA/flake8/commit/d31c5356bbb0a884555662185697ddc6bb46a44c"><code>d31c535</code></a> Release 4.0.0</li>
<li><a href="https://github.com/PyCQA/flake8/commit/afd2399b4cc9b27c4e8a5c2dec8444df8f480293"><code>afd2399</code></a> Merge pull request <a href="https://github-redirect.dependabot.com/pycqa/flake8/issues/1407">#1407</a> from asottile/setup-cfg-fmt</li>
<li><a href="https://github.com/PyCQA/flake8/commit/960cf8cf2044359d5fbd3454a2a9a1d7a0586594"><code>960cf8c</code></a> rerun setup-cfg-fmt (and restore comments)</li>
<li><a href="https://github.com/PyCQA/flake8/commit/d7baba5f14091e7975d2abb3ba9bf321b5be6102"><code>d7baba5</code></a> Merge pull request <a href="https://github-redirect.dependabot.com/pycqa/flake8/issues/1406">#1406</a> from asottile/update-versions</li>
<li><a href="https://github.com/PyCQA/flake8/commit/d79021aafc809d999c4cbbc0a513a5ceb473efa2"><code>d79021a</code></a> update dependency versions</li>
<li><a href="https://github.com/PyCQA/flake8/commit/283f0c81241673221d9628beb11e2d7356826f00"><code>283f0c8</code></a> Merge pull request <a href="https://github-redirect.dependabot.com/pycqa/flake8/issues/1404">#1404</a> from PyCQA/drop-xdg-config</li>
<li><a href="https://github.com/PyCQA/flake8/commit/807904aebc20814ac595b0004ab526fffb5ef681"><code>807904a</code></a> Drop support for Home and XDG config files</li>
<li>Additional commits viewable in <a href="https://github.com/pycqa/flake8/compare/0.1...4.0.1">compare view</a></li>
</ul>
</details>
<br />


Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself. You can also trigger a rebase manually by commenting `@dependabot rebase`.

[//]: # (dependabot-automerge-start)
[//]: # (dependabot-automerge-end)

---

<details>
<summary>Dependabot commands and options</summary>
<br />

You can trigger Dependabot actions by commenting on this PR:
- `@dependabot rebase` will rebase this PR
- `@dependabot recreate` will recreate this PR, overwriting any edits that have been made to it
- `@dependabot merge` will merge this PR after your CI passes on it
- `@dependabot squash and merge` will squash and merge this PR after your CI passes on it
- `@dependabot cancel merge` will cancel a previously requested merge and block automerging
- `@dependabot reopen` will reopen this PR if it is closed
- `@dependabot close` will close this PR and stop Dependabot recreating it. You can achieve the same result by closing it manually
- `@dependabot ignore this major version` will close this PR and stop Dependabot creating any more for this major version (unless you reopen the PR or upgrade to it yourself)
- `@dependabot ignore this minor version` will close this PR and stop Dependabot creating any more for this minor version (unless you reopen the PR or upgrade to it yourself)
- `@dependabot ignore this dependency` will close this PR and stop Dependabot creating any more for this dependency (unless you reopen the PR or upgrade to it yourself)


</details>
"""

    expected_title = "chore(deps-dev): update flake8 requirement from <4 to <5 (#6)"
    expected_body = """Updates the requirements on [flake8](https://github.com/pycqa/flake8) to permit the latest version.

Commits
* [`82b698e`](https://github.com/PyCQA/flake8/commit/82b698e09996cdde5d473e234681d8380810d7a2) Release 4.0.1
* [`0fac346`](https://github.com/PyCQA/flake8/commit/0fac346d8437d205e508643253c7a7d5fdf5dee7) Merge pull request [#1410](https://github-redirect.dependabot.com/pycqa/flake8/issues/1410) from PyCQA/parallel-syntax-error
* [`aa54693`](https://github.com/PyCQA/flake8/commit/aa54693c9ec03368c6e592efff4dd4757dd72a47) fix parallel execution collecting a SyntaxError
* [`d31c535`](https://github.com/PyCQA/flake8/commit/d31c5356bbb0a884555662185697ddc6bb46a44c) Release 4.0.0
* [`afd2399`](https://github.com/PyCQA/flake8/commit/afd2399b4cc9b27c4e8a5c2dec8444df8f480293) Merge pull request [#1407](https://github-redirect.dependabot.com/pycqa/flake8/issues/1407) from asottile/setup-cfg-fmt
* [`960cf8c`](https://github.com/PyCQA/flake8/commit/960cf8cf2044359d5fbd3454a2a9a1d7a0586594) rerun setup-cfg-fmt (and restore comments)
* [`d7baba5`](https://github.com/PyCQA/flake8/commit/d7baba5f14091e7975d2abb3ba9bf321b5be6102) Merge pull request [#1406](https://github-redirect.dependabot.com/pycqa/flake8/issues/1406) from asottile/update-versions
* [`d79021a`](https://github.com/PyCQA/flake8/commit/d79021aafc809d999c4cbbc0a513a5ceb473efa2) update dependency versions
* [`283f0c8`](https://github.com/PyCQA/flake8/commit/283f0c81241673221d9628beb11e2d7356826f00) Merge pull request [#1404](https://github-redirect.dependabot.com/pycqa/flake8/issues/1404) from PyCQA/drop-xdg-config
* [`807904a`](https://github.com/PyCQA/flake8/commit/807904aebc20814ac595b0004ab526fffb5ef681) Drop support for Home and XDG config files
* Additional commits viewable in [compare view](https://github.com/pycqa/flake8/compare/0.1...4.0.1)



  



Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself. You can also trigger a rebase manually by commenting `@dependabot rebase`.

[//]: # (dependabot-automerge-start)
[//]: # (dependabot-automerge-end)

---


Dependabot commands and options
  


You can trigger Dependabot actions by commenting on this PR:
- `@dependabot rebase` will rebase this PR
- `@dependabot recreate` will recreate this PR, overwriting any edits that have been made to it
- `@dependabot merge` will merge this PR after your CI passes on it
- `@dependabot squash and merge` will squash and merge this PR after your CI passes on it
- `@dependabot cancel merge` will cancel a previously requested merge and block automerging
- `@dependabot reopen` will reopen this PR if it is closed
- `@dependabot close` will close this PR and stop Dependabot recreating it. You can achieve the same result by closing it manually
- `@dependabot ignore this major version` will close this PR and stop Dependabot creating any more for this major version (unless you reopen the PR or upgrade to it yourself)
- `@dependabot ignore this minor version` will close this PR and stop Dependabot creating any more for this minor version (unless you reopen the PR or upgrade to it yourself)
- `@dependabot ignore this dependency` will close this PR and stop Dependabot creating any more for this dependency (unless you reopen the PR or upgrade to it yourself)



"""  # noqa
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await ctxt.pull_request.get_commit_message(
        "{{ title }} (#{{ number }})\n\n{{ body | markdownify }}"
    ) == (expected_title, expected_body)


async def test_context_body_section(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request[
        "title"
    ] = "chore(deps-dev): update flake8 requirement <-- noway we commit this-->from <4 to <5"
    a_pull_request[
        "body"
    ] = """
### Description

My awesome section with a beautiful description
<!-- I hide a comment !!! -->
Fixes MRGFY-XXX

### Development

- [X] All checks must pass (Semantic Pull Request, pep8, requirements, unit tests, functional tests, security checks, …)
- [X] The code changed/added as part of this pull request must be covered with tests
- [X] Features must have a link to a Linear task
- [X] Hotfixes must have a link to Sentry issue and the ``hotfix`` label

### Code Review

Code review policies are handled and automated by Mergify.

* When all tests pass, reviewers will be assigned automatically.
* When change is approved by at least one review and no pending review are
  remaining, pull request is retested against its base branch.
* The pull request is then merged automatically.

"""

    expected_title = "chore(deps-dev): update flake8 requirement from <4 to <5 (#6)"
    expected_body = """My awesome section with a beautiful description

Fixes MRGFY-XXX"""
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await ctxt.pull_request.get_commit_message(
        "{{ title | striptags }} (#{{ number }})\n\n{{ body | get_section('### Description') }}",
    ) == (expected_title, expected_body)


async def test_context_unexisting_section(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert (
        await ctxt.pull_request.get_commit_message(
            "{{ body | get_section('### Description', '') }}",
        )
        is None
    )


async def test_context_unexisting_section_with_templated_default(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await ctxt.pull_request.get_commit_message(
        "{{ body | get_section('### Description', '{{number}}\n{{author}}') }}",
    ) == (str(a_pull_request["number"]), a_pull_request["user"]["login"])


async def test_context_body_section_with_template(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request[
        "body"
    ] = """

Yo!

### Commit

BODY OF #{{number}}

"""
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    assert await ctxt.pull_request.get_commit_message(
        "TITLE\n{{ body | get_section('### Commit') }}",
    ) == ("TITLE", f"BODY OF #{a_pull_request['number']}")


async def test_context_body_section_with_bad_template(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    a_pull_request[
        "body"
    ] = """
Description
---

Test Plan
---

Instructions
---

"""
    ctxt = await context.Context.create(mock.Mock(), a_pull_request)
    with pytest.raises(context.RenderTemplateFailure):
        await ctxt.pull_request.get_commit_message(
            "TITLE\n{{ body | get_section('### Commit') }}",
        )


async def test_check_runs_ordering(
    a_pull_request: github_types.GitHubPullRequest,
) -> None:
    repo = mock.Mock()
    repo.get_branch_protection.side_effect = mock.AsyncMock(return_value=None)
    repo.installation.client.items = mock.MagicMock(__aiter__=[])
    ctxt = await context.Context.create(repo, a_pull_request)
    with mock.patch(
        "mergify_engine.check_api.get_checks_for_ref",
        return_value=[
            {
                "id": 6928770773,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "0ccccc7a-9630-5914-467b-15a9c61f0287",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "Publish release",
                "status": "completed",
                "output": {
                    "title": None,
                    "summary": "",
                    "text": None,
                    "annotations_count": 0,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "skipped",
                "completed_at": "2022-06-17T01:10:22Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928770773?check_suite_focus=true",
            },
            {
                "id": 6928212025,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "6614165d-8168-5766-9db6-f35f537f8e36",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "ci",
                "status": "completed",
                "output": {
                    "title": None,
                    "summary": "",
                    "text": None,
                    "annotations_count": 0,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "skipped",
                "completed_at": "2022-06-17T00:09:57Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928212025?check_suite_focus=true",
            },
            {
                "id": 6928210780,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "2a132eb7-5003-5af1-9f28-e7804a9048f4",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "mima",
                "status": "completed",
                "output": {
                    "title": None,
                    "summary": "",
                    "text": None,
                    "annotations_count": 0,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "success",
                "completed_at": "2022-06-17T00:23:06Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928210780?check_suite_focus=true",
            },
            {
                "id": 6928210606,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "af160194-4251-5b45-90f2-db2e90a93f16",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "ci (Native)",
                "status": "completed",
                "output": {
                    "title": None,
                    "summary": "",
                    "text": None,
                    "annotations_count": 0,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "success",
                "completed_at": "2022-06-17T01:10:20Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928210606?check_suite_focus=true",
            },
            {
                "id": 6928210520,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "ce3d09a6-c2ac-5c0d-cd25-ca5c547abb3a",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "ci (JS)",
                "status": "completed",
                "output": {
                    "title": None,
                    "summary": "",
                    "text": None,
                    "annotations_count": 0,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "success",
                "completed_at": "2022-06-17T00:44:12Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928210520?check_suite_focus=true",
            },
            {
                "id": 6928210396,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "980a8645-8e3e-5677-ba14-1ce4199013e8",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "ci (JVM)",
                "status": "completed",
                "output": {
                    "title": "ci (JVM)",
                    "summary": "",
                    "text": None,
                    "annotations_count": 1,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": "success",
                "completed_at": "2022-06-17T01:08:00Z",
                "html_url": "https://github.com/softwaremill/tapir/runs/6928210396?check_suite_focus=true",
            },
            {
                "id": 6928210396,
                "app_id": 15368,
                "app_name": "GitHub Actions",
                "app_avatar_url": "https://avatars.githubusercontent.com/u/9919?v=4",
                "external_id": "980a8645-8e3e-5677-ba14-1ce4199013e8",
                "head_sha": "d47d59a4723567c294c84c29336e2777038e7e30",
                "name": "ci (JVM)",
                "status": "in_progress",
                "output": {
                    "title": "ci (JVM)",
                    "summary": "",
                    "text": None,
                    "annotations_count": 1,
                    "annotations_url": "",
                    "annotations": [],
                },
                "conclusion": None,
                "completed_at": None,
                "html_url": "https://github.com/softwaremill/tapir/runs/6928210396?check_suite_focus=true",
            },
        ],
    ):
        assert await ctxt.checks == {
            "Publish release": "skipped",
            "ci": "skipped",
            "ci (JS)": "success",
            "ci (JVM)": None,
            "ci (Native)": "success",
            "mima": "success",
        }
