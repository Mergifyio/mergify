import asyncio
import functools
import logging
import os
import typing

import freezegun
import freezegun.api
import msgpack
import pytest
import respx

from mergify_engine import config
from mergify_engine import logs
from mergify_engine import redis_utils
from mergify_engine.clients import github


# for jwt generation
freezegun.configure(  # type:ignore[attr-defined]
    extend_ignore_list=["mergify_engine.clients.github_app"]
)


def msgpack_freezegun_fixes(obj: typing.Any) -> typing.Any:
    # NOTE(sileht): msgpack isinstance check doesn't support override of
    # __instancecheck__ like freezegun does, so we convert the fake one into a
    # real one
    if isinstance(obj, freezegun.api.FakeDatetime):  # type: ignore[attr-defined]
        return freezegun.api.real_datetime(  # type: ignore[attr-defined]
            obj.year,
            obj.month,
            obj.day,
            obj.hour,
            obj.minute,
            obj.second,
            obj.microsecond,
            obj.tzinfo,
        )
    return obj


# serialize freezegum FakeDatetime as datetime
msgpack.packb = functools.partial(msgpack.packb, default=msgpack_freezegun_fixes)

original_os_environ = os.environ.copy()


@pytest.fixture()
def original_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> typing.Generator[None, None, None]:
    current = os.environ.copy()
    os.environ.clear()
    os.environ.update(original_os_environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(current)


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


@pytest.fixture(autouse=True, scope="session")
def enable_api() -> None:
    config.API_ENABLE = True


@pytest.fixture()
async def redis_links() -> typing.AsyncGenerator[redis_utils.RedisLinks, None]:
    links = redis_utils.RedisLinks(name="global-fixture")
    await links.flushall()
    try:
        yield links
    finally:
        await links.flushall()
        await links.shutdown_all()


@pytest.fixture()
async def redis_cache(
    redis_links: redis_utils.RedisLinks,
) -> redis_utils.RedisCache:
    return redis_links.cache


@pytest.fixture()
async def redis_stream(
    redis_links: redis_utils.RedisLinks,
) -> redis_utils.RedisStream:
    return redis_links.stream


@pytest.fixture()
async def github_server(
    monkeypatch: pytest.MonkeyPatch,
) -> typing.AsyncGenerator[respx.MockRouter, None]:
    monkeypatch.setattr(github.CachedToken, "STORAGE", {})
    async with respx.mock(base_url=config.GITHUB_REST_API_URL) as respx_mock:
        respx_mock.post("/app/installations/12345/access_tokens").respond(
            200, json={"token": "<app_token>", "expires_at": "2100-12-31T23:59:59Z"}
        )
        yield respx_mock
