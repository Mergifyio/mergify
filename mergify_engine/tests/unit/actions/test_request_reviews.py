# Copyright © 2020 Mergify SAS
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
import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine.actions import request_reviews
from mergify_engine.clients import http


@pytest.mark.parametrize(
    "config",
    (
        {},
        {
            "users": ["hello"],
        },
        {
            "teams": ["hello", "@foobar"],
        },
    ),
)
def test_config(config):
    request_reviews.RequestReviewsAction.get_schema()(config)


def test_random_reviewers():
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 1,
            },
        },
    )
    reviewers = action._get_random_reviewers(123, "jd")
    assert reviewers == {"@foobar", "sileht"}
    reviewers = action._get_random_reviewers(124, "sileht")
    assert reviewers == {"jd", "@foobar"}
    reviewers = action._get_random_reviewers(124, "jd")
    assert reviewers == {"@foobaz", "@foobar"}


def test_random_reviewers_no_weight():
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": ["jd", "sileht"],
        },
    )
    reviewers = action._get_random_reviewers(123, "another-jd")
    assert reviewers == {"sileht", "jd"}
    reviewers = action._get_random_reviewers(124, "another-jd")
    assert reviewers == {"sileht", "@foobar"}
    reviewers = action._get_random_reviewers(124, "sileht")
    assert reviewers == {"@foobaz", "@foobar"}


def test_random_reviewers_count_bigger():
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 15,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 45,
            },
        }
    )
    reviewers = action._get_random_reviewers(123, "foobar")
    assert reviewers == {"@foobar", "@foobaz", "jd", "sileht"}
    reviewers = action._get_random_reviewers(124, "another-jd")
    assert reviewers == {"@foobar", "@foobaz", "jd", "sileht"}
    reviewers = action._get_random_reviewers(124, "jd")
    assert reviewers == {"@foobar", "@foobaz", "sileht"}


def test_random_config_too_much_count():
    with pytest.raises(voluptuous.MultipleInvalid) as p:
        request_reviews.RequestReviewsAction.get_schema()(
            {
                "random_count": 20,
                "teams": {
                    "foobar": 2,
                    "foobaz": 1,
                },
                "users": {
                    "foobar": 2,
                    "foobaz": 1,
                },
            },
        )
    assert (
        str(p.value)
        == "value must be at most 15 for dictionary value @ data['random_count']"
    )


def test_get_reviewers():
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 1,
            },
        },
    )
    reviewers = action._get_reviewers(843, set(), "another-jd")
    assert reviewers == ({"jd", "sileht"}, set())
    reviewers = action._get_reviewers(844, set(), "another-jd")
    assert reviewers == ({"jd"}, {"foobar"})
    reviewers = action._get_reviewers(845, set(), "another-jd")
    assert reviewers == ({"sileht"}, {"foobar"})
    reviewers = action._get_reviewers(845, {"sileht"}, "another-jd")
    assert reviewers == (set(), {"foobar"})
    reviewers = action._get_reviewers(845, {"jd"}, "another-jd")
    assert reviewers == ({"sileht"}, {"foobar"})
    reviewers = action._get_reviewers(845, set(), "SILEHT")
    assert reviewers == ({"jd"}, {"foobar"})


async def prepare_context(client, redis_cache, subscribed=True):
    sub = subscription.Subscription(
        redis_cache,
        123,
        subscribed,
        "sub or not to sub",
        frozenset(
            getattr(subscription.Features, f) for f in subscription.Features.__members__
        )
        if subscribed
        else frozenset(),
    )

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
    installation = context.Installation(123, "Mergifyio", sub, client, redis_cache)
    repository = context.Repository(installation, gh_repo)
    return await context.Context.create(
        repository,
        {
            "id": 12345,
            "number": 123,
            "state": None,
            "mergeable_state": "ok",
            "merged_by": None,
            "merged": None,
            "merged_at": None,
            "user": {"login": "jd"},
            "requested_reviewers": [{"login": "jd"}, {"login": "sileht"}],
            "requested_teams": [{"slug": "foobar"}, {"slug": "foobaz"}],
            "base": {
                "sha": "sha",
                "ref": "main",
                "user": {
                    "login": {
                        "Mergifyio",
                    },
                },
                "repo": {
                    "full_name": "Mergifyio/demo",
                    "name": "demo",
                    "private": False,
                    "owner": {
                        "login": "Mergifyio",
                    },
                },
            },
        },
    )


@pytest.mark.asyncio
async def test_disabled(redis_cache):
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 1,
            },
        },
    )
    client = mock.MagicMock()
    client.auth.installation.__getitem__.return_value = 123
    ctxt = await prepare_context(client, redis_cache, subscribed=False)
    result = await action.run(ctxt, None)
    assert result.conclusion == check_api.Conclusion.ACTION_REQUIRED
    assert result.title == "Random request reviews are disabled"
    assert result.summary == (
        "⚠ The [subscription](https://dashboard.mergify.io/github/Mergifyio/subscription) "
        "needs to be updated to enable this feature."
    )


@pytest.mark.asyncio
async def test_team_permissions_missing(redis_cache):
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "@other/foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 1,
            },
        },
    )
    client = mock.MagicMock()
    client.auth.installation.__getitem__.return_value = 123
    client.get = mock.AsyncMock(
        side_effect=http.HTTPNotFound(
            message="not found", response=mock.ANY, request=mock.ANY
        )
    )
    ctxt = await prepare_context(client, redis_cache)
    result = await action.run(ctxt, None)
    assert result.conclusion == check_api.Conclusion.FAILURE
    assert result.title == "Invalid requested teams"
    for error in (
        "Team `foobar` does not exist or has not access to this repository",
        "Team `@other/foobaz` is not part of the organization `Mergifyio`",
    ):
        assert error in result.summary


@pytest.mark.asyncio
async def test_team_permissions_ok(redis_cache):
    action = request_reviews.RequestReviewsAction.get_schema()(
        {
            "random_count": 2,
            "teams": {
                "foobar": 2,
                "foobaz": 1,
            },
            "users": {
                "jd": 2,
                "sileht": 1,
            },
        },
    )
    client = mock.MagicMock()
    client.auth.installation.__getitem__.return_value = 123
    client.get = mock.AsyncMock(return_value={})
    ctxt = await prepare_context(client, redis_cache)
    result = await action.run(ctxt, None)
    assert result.summary == ""
    assert result.title == "No new reviewers to request"
    assert result.conclusion == check_api.Conclusion.SUCCESS
