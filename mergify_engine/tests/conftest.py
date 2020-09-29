import logging

import pytest

from mergify_engine import logs


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
        # NOTE(sileht): The asyncio task spawned to automatically close redis connection
        # cleanly is not held by an variable, making hard to track them. Since this is one
        # annoying for testing just ignore message about them.
        messages = [
            m
            for m in messages
            if "coro=<ConnectionPool.disconnect_on_idle_time_exceeded()" not in m
        ]
        assert [] == messages
