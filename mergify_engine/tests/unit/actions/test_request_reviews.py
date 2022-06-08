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
import typing
from unittest import mock

import pytest
import voluptuous

from mergify_engine import check_api
from mergify_engine import github_types
from mergify_engine.actions import request_reviews
from mergify_engine.clients import http
from mergify_engine.tests.unit import conftest


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
def test_config(config: typing.Dict[str, typing.List[str]]) -> None:
    request_reviews.RequestReviewsAction.get_schema()(config)


def test_random_reviewers() -> None:
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


def test_random_reviewers_no_weight() -> None:
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


def test_random_reviewers_count_bigger() -> None:
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


def test_random_config_too_much_count() -> None:
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


def test_get_reviewers() -> None:
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


async def test_disabled(context_getter: conftest.ContextGetterFixture) -> None:
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
    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    result = await action.run(ctxt, None)
    assert result.conclusion == check_api.Conclusion.ACTION_REQUIRED
    assert result.title == "Random request reviews are disabled"
    assert result.summary == (
        "⚠ The [subscription](https://dashboard.mergify.com/github/Mergifyio/subscription) "
        "needs to be updated to enable this feature."
    )


@pytest.mark.subscription
async def test_team_permissions_missing(
    context_getter: conftest.ContextGetterFixture,
) -> None:
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
    client.get = mock.AsyncMock(
        side_effect=http.HTTPNotFound(
            message="not found", response=mock.ANY, request=mock.ANY
        )
    )
    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client
    result = await action.run(ctxt, None)
    assert result.conclusion == check_api.Conclusion.ACTION_REQUIRED
    assert result.title == "Invalid requested teams"
    for error in (
        "Team `foobar` does not exist or has not access to this repository",
        "Team `@other/foobaz` is not part of the organization `Mergifyio`",
    ):
        assert error in result.summary


@pytest.mark.subscription
async def test_team_permissions_ok(
    context_getter: conftest.ContextGetterFixture,
) -> None:
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
    client.get = mock.AsyncMock(return_value={})
    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client
    result = await action.run(ctxt, None)
    assert result.summary == ""
    assert result.title == "No new reviewers to request"
    assert result.conclusion == check_api.Conclusion.SUCCESS
