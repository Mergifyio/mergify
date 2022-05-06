# -*- encoding: utf-8 -*-
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

import functools
import typing
from unittest import mock

import httpx
import pytest

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.dashboard import subscription
from mergify_engine.web import root as web_root


@pytest.fixture
def fake_subscription(
    redis_cache: redis_utils.RedisCache,
    request: pytest.FixtureRequest,
) -> subscription.Subscription:
    marker = request.node.get_closest_marker("subscription")
    subscribed = marker is not None
    return subscription.Subscription(
        redis_cache,
        123,
        "sub or not to sub",
        frozenset(
            getattr(subscription.Features, f) for f in subscription.Features.__members__
        )
        if subscribed
        else frozenset([subscription.Features.PUBLIC_REPOSITORY]),
    )


@pytest.fixture
def fake_repository(
    redis_links: redis_utils.RedisLinks,
    fake_subscription: subscription.Subscription,
) -> context.Repository:
    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("Mergifyio"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "Mergifyio/mergify-engine",
            "name": github_types.GitHubRepositoryName("mergify-engine"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("main"),
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

    fake_client = mock.Mock()
    installation = context.Installation(
        installation_json, fake_subscription, fake_client, redis_links
    )
    return context.Repository(installation, gh_repo)


async def build_fake_context(
    number: github_types.GitHubPullRequestNumber,
    *,
    repository: context.Repository,
    **kwargs: typing.Dict[str, typing.Any],
) -> context.Context:
    pull_request_author = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(123),
            "type": "User",
            "login": github_types.GitHubLogin("contributor"),
            "avatar_url": "",
        }
    )

    pull: github_types.GitHubPullRequest = {
        "node_id": "42",
        "locked": False,
        "assignees": [],
        "requested_reviewers": [
            {
                "id": github_types.GitHubAccountIdType(123),
                "type": "User",
                "login": github_types.GitHubLogin("jd"),
                "avatar_url": "",
            },
            {
                "id": github_types.GitHubAccountIdType(456),
                "type": "User",
                "login": github_types.GitHubLogin("sileht"),
                "avatar_url": "",
            },
        ],
        "requested_teams": [
            {"slug": github_types.GitHubTeamSlug("foobar")},
            {"slug": github_types.GitHubTeamSlug("foobaz")},
        ],
        "milestone": None,
        "title": "awesome",
        "body": "",
        "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
        "closed_at": None,
        "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
        "id": github_types.GitHubPullRequestId(123),
        "maintainer_can_modify": True,
        "user": pull_request_author,
        "labels": [],
        "rebaseable": True,
        "draft": False,
        "merge_commit_sha": None,
        "number": number,
        "commits": 1,
        "mergeable_state": "clean",
        "mergeable": True,
        "state": "open",
        "changed_files": 1,
        "head": {
            "sha": github_types.SHAType("the-head-sha"),
            "label": github_types.GitHubHeadBranchLabel(
                f"{pull_request_author['login']}:feature-branch"
            ),
            "ref": github_types.GitHubRefType("feature-branch"),
            "repo": {
                "id": github_types.GitHubRepositoryIdType(123),
                "default_branch": github_types.GitHubRefType("main"),
                "name": github_types.GitHubRepositoryName("mergify-engine"),
                "full_name": "contributor/mergify-engine",
                "archived": False,
                "private": False,
                "owner": pull_request_author,
                "url": "https://api.github.com/repos/contributor/mergify-engine",
                "html_url": "https://github.com/contributor/mergify-engine",
            },
            "user": pull_request_author,
        },
        "merged": False,
        "merged_by": None,
        "merged_at": None,
        "html_url": "https://...",
        "base": {
            "label": github_types.GitHubBaseBranchLabel("mergify_engine:main"),
            "ref": github_types.GitHubRefType("main"),
            "repo": repository.repo,
            "sha": github_types.SHAType("the-base-sha"),
            "user": repository.repo["owner"],
        },
    }
    pull.update(kwargs)  # type: ignore
    return await context.Context.create(repository, pull)


ContextGetterFixture = typing.Callable[
    ..., typing.Coroutine[typing.Any, typing.Any, context.Context]
]


@pytest.fixture
def context_getter(fake_repository: context.Repository) -> ContextGetterFixture:
    return functools.partial(build_fake_context, repository=fake_repository)


@pytest.fixture
async def mergify_web_client() -> typing.AsyncGenerator[httpx.AsyncClient, None]:
    await web_root.startup()
    client = httpx.AsyncClient(app=web_root.app, base_url="http://localhost")
    try:
        yield client
    finally:
        await client.aclose()
        await web_root.shutdown()
