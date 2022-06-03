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

import asyncio

import httpx
import pytest

from mergify_engine import web_cli
from mergify_engine.tests.functional import conftest as func_conftest


@pytest.mark.recorder
def test_clear_token_cache(
    mergify_web_client: httpx.AsyncClient,
    dashboard: func_conftest.DashboardFixture,
    monkeypatch: pytest.MonkeyPatch,
    recorder: func_conftest.RecorderFixture,
    event_loop: asyncio.BaseEventLoop,
) -> None:
    monkeypatch.setattr("asyncio.run", lambda coro: event_loop.run_until_complete(coro))
    monkeypatch.setattr("mergify_engine.web_cli.config.BASE_URL", "http://localhost")
    monkeypatch.setattr(
        "mergify_engine.web_cli.http.AsyncClient", lambda: mergify_web_client
    )
    monkeypatch.setattr(
        "sys.argv",
        ["mergify-clear-token-cache", str(recorder.config["organization_id"])],
    )
    web_cli.clear_token_cache()


@pytest.mark.recorder
def test_refresher(
    mergify_web_client: httpx.AsyncClient,
    dashboard: func_conftest.DashboardFixture,
    recorder: func_conftest.RecorderFixture,
    monkeypatch: pytest.MonkeyPatch,
    event_loop: asyncio.BaseEventLoop,
) -> None:
    monkeypatch.setattr("asyncio.run", lambda coro: event_loop.run_until_complete(coro))
    monkeypatch.setattr("mergify_engine.web_cli.config.BASE_URL", "http://localhost")
    monkeypatch.setattr(
        "mergify_engine.web_cli.http.AsyncClient", lambda: mergify_web_client
    )
    repo = (
        f"{recorder.config['organization_name']}/{recorder.config['repository_name']}"
    )
    monkeypatch.setattr("sys.argv", ["mergify-refresher", "--action=admin", repo])
    web_cli.refresher()
