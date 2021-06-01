import asyncio
import logging
import typing

import freezegun
import pytest
from pytest_httpserver import httpserver

from mergify_engine import config
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine.clients import github


# for jwt generation
freezegun.configure(  # type:ignore[attr-defined]
    extend_ignore_list=["mergify_engine.clients.github_app"]
)


@pytest.fixture()
def logger_checker(request, caplog):
    # daiquiri removes all handlers during setup, as we want to sexy output and the pytest
    # capability at the same, we must add back the pytest handler
    logs.setup_logging()
    logging.getLogger(None).addHandler(caplog.handler)
    yield
    for when in ("setup", "call", "teardown"):
        messages = [
            rec.getMessage()
            for rec in caplog.get_records(when)
            if rec.levelname in ("CRITICAL", "ERROR")
        ]
        assert [] == messages


@pytest.fixture(autouse=True)
def setup_new_event_loop() -> None:
    # ensure each tests have a fresh event loop
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture()
async def redis_cache() -> typing.AsyncGenerator[utils.RedisCache, None]:
    with utils.aredis_for_cache() as client:
        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            client.connection_pool.disconnect()
            await utils.stop_pending_aredis_tasks()


@pytest.fixture()
async def redis_stream() -> typing.AsyncGenerator[utils.RedisStream, None]:
    with utils.aredis_for_stream() as client:
        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            client.connection_pool.disconnect()
            await utils.stop_pending_aredis_tasks()


@pytest.fixture()
async def github_server(
    httpserver: httpserver.HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> typing.AsyncGenerator[httpserver.HTTPServer, None]:
    monkeypatch.setattr(config, "GITHUB_API_URL", httpserver.url_for("/")[:-1])
    monkeypatch.setattr(github.CachedToken, "STORAGE", {})

    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {
            "id": 12345,
            "target_type": "User",
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "account": {"login": "owner", "id": 12345},
        }
    )
    httpserver.expect_request(
        "/app/installations/12345/access_tokens"
    ).respond_with_json({"token": "<app_token>", "expires_at": "2100-12-31T23:59:59Z"})

    yield httpserver
