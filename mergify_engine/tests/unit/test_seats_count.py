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

from unittest import mock

import pytest
from pytest_httpserver import httpserver

from mergify_engine import count_seats


@pytest.mark.asyncio
async def test_send_seats(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request(
        "/on-premise/report", method="POST", json={"seats": 5}
    ).respond_with_data("Accepted", status=201)
    with mock.patch(
        "mergify_engine.config.SUBSCRIPTION_BASE_URL",
        httpserver.url_for("/")[:-1],
    ):
        await count_seats.send_seats(5)

    assert len(httpserver.log) == 1

    httpserver.check_assertions()
