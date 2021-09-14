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

import datetime
import json
import os
from unittest import mock

from freezegun import freeze_time
import httpx
import pytest
from pytest_httpserver import httpserver

from mergify_engine import count_seats
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.web import redis
from mergify_engine.web import root


def test_seats_renamed_account_repo() -> None:
    user1 = count_seats.SeatAccount(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user1"),
    )
    user1bis = count_seats.SeatAccount(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user1bis"),
    )
    user2 = count_seats.SeatAccount(
        github_types.GitHubAccountIdType(456),
        github_types.GitHubLogin("user2"),
    )
    user2bis = count_seats.SeatAccount(
        github_types.GitHubAccountIdType(456),
        github_types.GitHubLogin("user2bis"),
    )

    users = {user1, user2, user2bis, user1bis}
    assert len(users) == 2
    assert list(users)[0].login == "user2"
    assert list(users)[1].login == "user1"

    repo1 = count_seats.SeatRepository(
        github_types.GitHubRepositoryIdType(123),
        github_types.GitHubRepositoryName("repo1"),
    )
    repo1bis = count_seats.SeatRepository(
        github_types.GitHubRepositoryIdType(123),
        github_types.GitHubRepositoryName("repo1bis"),
    )
    repo2 = count_seats.SeatRepository(
        github_types.GitHubRepositoryIdType(456),
        github_types.GitHubRepositoryName("repo2"),
    )
    repo2bis = count_seats.SeatRepository(
        github_types.GitHubRepositoryIdType(456),
        github_types.GitHubRepositoryName("repo2bis"),
    )

    repos = {repo1, repo2, repo2bis, repo1bis}
    assert repos == {repo1, repo2}


@pytest.mark.asyncio
async def test_send_seats(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request(
        "/on-premise/report",
        method="POST",
        json={"seats": 5, "write_users": 5, "active_users": 2},
    ).respond_with_data("Accepted", status=201)
    with mock.patch(
        "mergify_engine.config.SUBSCRIPTION_BASE_URL",
        httpserver.url_for("/")[:-1],
    ):
        await count_seats.send_seats(count_seats.SeatsCountResultT(5, 2))

    assert len(httpserver.log) == 1

    httpserver.check_assertions()


GITHUB_SAMPLE_EVENTS = {}
_EVENT_DIR = os.path.join(os.path.dirname(__file__), "events")
for filename in os.listdir(_EVENT_DIR):
    event_type = filename.split(".")[0]
    with open(os.path.join(_EVENT_DIR, filename), "r") as event:
        GITHUB_SAMPLE_EVENTS[filename] = (event_type, json.load(event))


@freeze_time("2011-11-11")
@pytest.mark.asyncio
@pytest.mark.parametrize("event_type, event", list(GITHUB_SAMPLE_EVENTS.values()))
async def test_store_active_users(event_type, event, redis_cache):
    await count_seats.store_active_users(redis_cache, event_type, event)
    one_month_ago = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    if event_type == "push":
        assert await redis_cache.zrangebyscore(
            "active-users~21031067~Codertocat~186853002~Hello-World",
            min=one_month_ago.timestamp(),
            max="+inf",
            withscores=True,
        ) == [
            ("21031067~Codertocat", 1320969600.0),
        ]
    elif event_type == "pull_request":
        assert await redis_cache.zrangebyscore(
            "active-users~21031067~Codertocat~186853002~Hello-World",
            min=one_month_ago.timestamp(),
            max="+inf",
            withscores=True,
        ) == [
            ("12345678~AnotherUser", 1320969600.0),
            ("21031067~Codertocat", 1320969600.0),
        ]
    else:
        assert (
            await redis_cache.zrangebyscore(
                "active-users~21031067~Codertocat~186853002~Hello-World",
                min=one_month_ago.timestamp(),
                max="+inf",
            )
            == []
        )


@freeze_time("2011-11-11")
@pytest.mark.asyncio
@pytest.mark.parametrize("event_type, event", list(GITHUB_SAMPLE_EVENTS.values()))
async def test_get_usage(event_type, event, redis_cache):
    await (count_seats.store_active_users(redis_cache, event_type, event))
    charset = "utf8"
    await redis.startup()
    async with httpx.AsyncClient(base_url="http://whatever", app=root.app) as client:
        data = b"a" * 123
        headers = {
            "X-Hub-Signature": f"sha1={utils.compute_hmac(data)}",
            "Content-Type": f"application/json; charset={charset}",
        }
        reply = await client.request(
            "GET", "/organization/1234/usage", content=data, headers=headers
        )
        assert reply.status_code == 200, reply.content
        assert json.loads(reply.content) == {"repositories": []}

        reply = await client.request(
            "GET", "/organization/21031067/usage", content=data, headers=headers
        )
        assert reply.status_code == 200, reply.content
        if event_type == "pull_request":
            assert json.loads(reply.content) == {
                "repositories": [
                    {
                        "collaborators": {
                            "active_users": [
                                {"id": 21031067, "login": "Codertocat"},
                                {"id": 12345678, "login": "AnotherUser"},
                            ],
                            "write_users": None,
                        },
                        "id": 186853002,
                        "name": "Hello-World",
                    }
                ],
            }
        elif event_type == "push":
            assert json.loads(reply.content) == {
                "repositories": [
                    {
                        "collaborators": {
                            "active_users": [
                                {"id": 21031067, "login": "Codertocat"},
                            ],
                            "write_users": None,
                        },
                        "id": 186853002,
                        "name": "Hello-World",
                    }
                ],
            }

        else:
            assert json.loads(reply.content) == {"repositories": []}
