import asyncio
import logging

import pytest

from mergify_engine import logs
from mergify_engine import utils


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
def setup_new_event_loop():
    # ensure each tests have a fresh event loop
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture()
async def redis_cache():
    async with utils.aredis_for_cache() as client:
        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            client.connection_pool.disconnect()
            await utils.stop_pending_aredis_tasks()


@pytest.fixture()
async def redis_stream():
    async with utils.aredis_for_stream() as client:
        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            client.connection_pool.disconnect()
            await utils.stop_pending_aredis_tasks()
