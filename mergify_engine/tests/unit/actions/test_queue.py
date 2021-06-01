# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine.actions import merge_base


def pull_request_rule_from_list(lst: typing.Any) -> rules.PullRequestRules:
    return typing.cast(
        rules.PullRequestRules,
        voluptuous.Schema(rules.get_pull_request_rules_schema())(lst),
    )


@pytest.fixture
def fake_client():
    async def items_call(url, *args, **kwargs):
        if url == "/repos/user/name/commits/azertyu/status":
            return
        elif url == "/repos/user/name/commits/azertyu/check-runs":
            yield {
                "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                "details_url": "https://example.com",
                "status": "completed",
                "conclusion": "failure",
                "name": "failure",
            }
            yield {
                "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                "details_url": "https://example.com",
                "status": "completed",
                "conclusion": "success",
                "name": "success",
            }
            yield {
                "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                "details_url": "https://example.com",
                "status": "completed",
                "conclusion": "neutral",
                "name": "neutral",
            }
            yield {
                "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                "details_url": "https://example.com",
                "status": "in_progress",
                "conclusion": None,
                "name": "pending",
            }
        else:
            raise Exception(f"url not mocked: {url}")

    def item_call(url, *args, **kwargs):
        if url == "/repos/user/name/branches/master":
            return {"commit": {"sha": "sha1"}, "protection": {"enabled": False}}
        else:
            raise Exception(f"url not mocked: {url}")

    client = mock.Mock()
    client.item = mock.AsyncMock(side_effect=item_call)
    client.items = items_call
    return client


async def fake_context(repository, number, **kwargs):
    pull: github_types.GitHubPullRequest = {
        "locked": False,
        "assignees": [],
        "updated_at": github_types.ISODateTimeType(""),
        "requested_reviewers": [],
        "requested_teams": [],
        "milestone": None,
        "title": "awesome",
        "body": "",
        "id": 123,
        "maintainer_can_modify": True,
        "user": {
            "id": 123,
            "type": "Orgs",
            "login": "Mergifyio",
            "avatar_url": "",
        },
        "labels": [],
        "rebaseable": True,
        "draft": False,
        "merge_commit_sha": None,
        "number": number,
        "commits": 1,
        "mergeable_state": "clean",
        "state": "open",
        "changed_files": 1,
        "head": {
            "sha": "azertyu",
            "label": "Mergifyio:feature-branch",
            "ref": "feature-branch",
            "repo": {
                "id": 123,
                "default_branch": "master",
                "name": "mergify-engine",
                "full_name": "Mergifyio/mergify-engine",
                "archived": False,
                "private": False,
                "owner": {
                    "id": 123,
                    "type": "Orgs",
                    "login": "Mergifyio",
                    "avatar_url": "",
                },
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
                "html_url": "https://github.com/Mergifyio/mergify-engine",
            },
            "user": {
                "id": 123,
                "type": "Orgs",
                "login": "Mergifyio",
                "avatar_url": "",
            },
        },
        "merged": False,
        "merged_by": None,
        "merged_at": None,
        "html_url": "https://...",
        "base": {
            "label": "Mergifyio:master",
            "ref": "master",
            "repo": {
                "id": 123,
                "default_branch": "master",
                "name": "mergify-engine",
                "full_name": "Mergifyio/mergify-engine",
                "archived": False,
                "private": False,
                "owner": {
                    "id": 123,
                    "type": "Orgs",
                    "login": "Mergifyio",
                    "avatar_url": "",
                },
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
                "html_url": "https://github.com/Mergifyio/mergify-engine",
            },
            "sha": "miaou",
            "user": {
                "id": 123,
                "type": "Orgs",
                "login": "Mergifyio",
                "avatar_url": "",
            },
        },
    }
    pull.update(kwargs)
    return await context.Context.create(repository, pull)


@pytest.fixture
def repository(redis_cache, fake_client):
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
        fake_client,
        redis_cache,
    )
    return context.Repository(installation, gh_repo)


@pytest.mark.parametrize(
    "conditions,conclusion",
    (
        (
            ["title=awesome", "check-neutral:neutral", "check-success:success"],
            check_api.Conclusion.SUCCESS,
        ),
        (
            ["title!=awesome", "check-neutral:neutral", "check-success:success"],
            check_api.Conclusion.FAILURE,
        ),
        (
            ["title=awesome", "check-neutral:neutral", "check-success:pending"],
            check_api.Conclusion.PENDING,
        ),
        (
            ["title=awesome", "check-neutral:pending", "check-success:pending"],
            check_api.Conclusion.PENDING,
        ),
        (
            ["title=awesome", "check-neutral:notexists", "check-success:success"],
            check_api.Conclusion.PENDING,
        ),
        (
            ["title=awesome", "check-neutral:failure", "check-success:success"],
            check_api.Conclusion.FAILURE,
        ),
    ),
)
@pytest.mark.asyncio
async def test_get_rule_checks_status(
    conditions, conclusion, repository, logger_checker
):
    ctxt = await fake_context(repository, 1)
    rules = pull_request_rule_from_list(
        [
            {
                "name": "hello",
                "conditions": conditions,
                "actions": {},
            }
        ]
    )
    match = await rules.get_pull_request_rule(ctxt)
    evaluated_rule = match.matching_rules[0]
    assert (await merge_base.get_rule_checks_status(ctxt, evaluated_rule)) == conclusion
