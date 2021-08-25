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
import pytest
from pytest_httpserver import httpserver

from mergify_engine import count_seats


@pytest.mark.asyncio
async def test_send_seats(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request(
        "/on-premise/report",
        method="POST",
        json={"seats": 5, "seats_write": 5, "seats_active": 2},
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
    if event_type in (
        "pull_request",
        "push",
    ):
        print(await redis_cache.keys("active-users~*"))
        assert (
            await redis_cache.zrangebyscore(
                "active-users~21031067~Codertocat~186853002~Hello-World",
                min=one_month_ago.timestamp(),
                max="+inf",
                withscores=True,
            )
            == [("21031067~Codertocat", 1320969600.0)]
        )
    else:
        assert (
            await redis_cache.zrangebyscore(
                "active-users~21031067~Codertocat~186853002~Hello-World",
                min=one_month_ago.timestamp(),
                max="+inf",
            )
            == []
        )
