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
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.clients import http
from mergify_engine.rules import checks_status
from mergify_engine.tests.unit import conftest


def pull_request_rule_from_list(lst: typing.Any) -> rules.PullRequestRules:
    return typing.cast(
        rules.PullRequestRules,
        voluptuous.Schema(rules.get_pull_request_rules_schema())(lst),
    )


@pytest.fixture
def fake_client() -> mock.Mock:
    async def items_call(url, *args, **kwargs):
        if url == "/repos/Mergifyio/mergify-engine/commits/the-head-sha/status":
            return
        elif url == "/repos/Mergifyio/mergify-engine/commits/the-head-sha/check-runs":
            yield github_types.GitHubCheckRun(
                {
                    "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                    "details_url": "https://example.com",
                    "status": "completed",
                    "conclusion": "failure",
                    "name": "failure",
                    "id": 1234,
                    "app": {
                        "id": 1234,
                        "name": "CI",
                        "owner": {
                            "type": "User",
                            "id": 1234,
                            "login": "goo",
                            "avatar_url": "https://example.com",
                        },
                    },
                    "external_id": None,
                    "pull_requests": [],
                    "before": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "after": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "started_at": "",
                    "completed_at": None,
                    "html_url": "https://example.com",
                    "check_suite": {"id": 1234},
                    "output": {
                        "summary": "",
                        "title": "It runs!",
                        "text": "",
                        "annotations": [],
                        "annotations_count": 0,
                        "annotations_url": "https://example.com",
                    },
                }
            )
            yield github_types.GitHubCheckRun(
                {
                    "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                    "details_url": "https://example.com",
                    "status": "completed",
                    "conclusion": "success",
                    "name": "success",
                    "id": 1235,
                    "app": {
                        "id": 1234,
                        "name": "CI",
                        "owner": {
                            "type": "User",
                            "id": 1234,
                            "login": "goo",
                            "avatar_url": "https://example.com",
                        },
                    },
                    "external_id": None,
                    "pull_requests": [],
                    "before": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "after": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "started_at": "",
                    "completed_at": None,
                    "html_url": "https://example.com",
                    "check_suite": {"id": 1234},
                    "output": {
                        "summary": "",
                        "title": "It runs!",
                        "text": "",
                        "annotations": [],
                        "annotations_count": 0,
                        "annotations_url": "https://example.com",
                    },
                }
            )
            yield github_types.GitHubCheckRun(
                {
                    "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                    "details_url": "https://example.com",
                    "status": "completed",
                    "conclusion": "neutral",
                    "name": "neutral",
                    "id": 1236,
                    "app": {
                        "id": 1234,
                        "name": "CI",
                        "owner": {
                            "type": "User",
                            "id": 1234,
                            "login": "goo",
                            "avatar_url": "https://example.com",
                        },
                    },
                    "external_id": None,
                    "pull_requests": [],
                    "before": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "after": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "started_at": "",
                    "completed_at": None,
                    "html_url": "https://example.com",
                    "check_suite": {"id": 1234},
                    "output": {
                        "summary": "",
                        "title": "It runs!",
                        "text": "",
                        "annotations": [],
                        "annotations_count": 0,
                        "annotations_url": "https://example.com",
                    },
                }
            )
            yield github_types.GitHubCheckRun(
                {
                    "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                    "details_url": "https://example.com",
                    "status": "in_progress",
                    "conclusion": None,
                    "name": "pending",
                    "id": 1237,
                    "app": {
                        "id": 1234,
                        "name": "CI",
                        "owner": {
                            "type": "User",
                            "id": 1234,
                            "login": "goo",
                            "avatar_url": "https://example.com",
                        },
                    },
                    "external_id": None,
                    "pull_requests": [],
                    "before": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "after": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                    "started_at": "",
                    "completed_at": None,
                    "html_url": "https://example.com",
                    "check_suite": {"id": 1234},
                    "output": {
                        "summary": "",
                        "title": "It runs!",
                        "text": "",
                        "annotations": [],
                        "annotations_count": 0,
                        "annotations_url": "https://example.com",
                    },
                }
            )
        else:
            raise Exception(f"url not mocked: {url}")

    def item_call(url, *args, **kwargs):
        if url == "/repos/Mergifyio/mergify-engine/branches/main":
            return {"commit": {"sha": "sha1"}, "protection": {"enabled": False}}
        if url == "/repos/Mergifyio/mergify-engine/branches/main/protection":
            raise http.HTTPNotFound(
                message="boom", response=mock.Mock(), request=mock.Mock()
            )
        else:
            raise Exception(f"url not mocked: {url}")

    client = mock.Mock()
    client.item = mock.AsyncMock(side_effect=item_call)
    client.items = items_call
    return client


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
        (
            [
                {
                    "or": [
                        {"and": ["title=awesome", "check-success:pending"]},
                        {"and": ["title!=awesome", "check-success:pending"]},
                    ]
                }
            ],
            check_api.Conclusion.PENDING,
        ),
        (
            [
                {
                    "or": [
                        {"and": ["title!=awesome", "check-success:pending"]},
                        {"and": ["title=foobar", "check-success:pending"]},
                    ]
                }
            ],
            check_api.Conclusion.FAILURE,
        ),
    ),
)
async def test_get_rule_checks_status(
    conditions: typing.Any,
    conclusion: check_api.Conclusion,
    context_getter: conftest.ContextGetterFixture,
    fake_client: mock.Mock,
) -> None:
    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = fake_client
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
    assert (
        await checks_status.get_rule_checks_status(
            ctxt.log, ctxt.repository, [ctxt.pull_request], evaluated_rule
        )
    ) == conclusion
