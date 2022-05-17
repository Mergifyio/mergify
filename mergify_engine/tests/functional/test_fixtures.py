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
import httpx
import pytest

from mergify_engine import config
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.dashboard import subscription
from mergify_engine.tests.functional import conftest as func_conftest


async def test_fixture_mergify_web_client(
    mergify_web_client: httpx.AsyncClient,
) -> None:
    r = await mergify_web_client.get("/foobar")
    assert r.status_code == 404


@pytest.mark.recorder
async def test_fixture_recorder() -> None:
    async with github.AsyncGithubClient(auth=github_app.GithubBearerAuth()) as client:
        r = await client.get("/app")
        assert r.status_code == 200
        assert r.json()["owner"]["id"] == config.TESTING_ORGANIZATION_ID
        assert r.json()["owner"]["login"] == config.TESTING_ORGANIZATION_NAME


async def test_fixture_dashboard(dashboard: func_conftest.DashboardFixture) -> None:
    assert dashboard.subscription.features == frozenset(
        {
            subscription.Features.PUBLIC_REPOSITORY,
            subscription.Features.EVENTLOGS_SHORT,
            subscription.Features.EVENTLOGS_LONG,
        }
    )
