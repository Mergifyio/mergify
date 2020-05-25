# -*- encoding: utf-8 -*-
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
import sys
import time
from unittest import mock

from freezegun import freeze_time
import pytest

from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import http


if sys.version_info < (3, 8):
    # https://github.com/pytest-dev/pytest-asyncio/issues/69
    pytest.skip(
        "mock + pytest-asyncio requires python3.8 or higher", allow_module_level=True,
    )


@pytest.fixture()
async def redis():
    r = await utils.create_aredis_for_stream()
    await r.flushdb()
    try:
        yield r
    finally:
        await r.flushdb()
        r.connection_pool.disconnect()


async def run_worker():
    w = worker.Worker()
    w.start()
    timeout = 10
    started_at = time.monotonic()
    while (
        w._redis is None or (await w._redis.zcard("streams")) > 0
    ) and time.monotonic() - started_at < timeout:
        await asyncio.sleep(0.5)
    w.stop()
    await w.wait_shutdown_complete()


fake_subscription = {}


def fake_install_id(installation_id):
    return {"id": installation_id, "account": {"login": "testing"}}


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_waiting_tasks(
    run_engine, get_install_by_id, redis, logger_checker
):
    get_install_by_id.side_effect = fake_install_id
    stream_names = []
    for installation_id in range(8):
        for pull_number in range(2):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                stream_names.append(f"stream~{installation_id}")
                await worker.push(
                    redis,
                    installation_id,
                    owner,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    # Check everything we push are in redis
    assert 8 == (await redis.zcard("streams"))
    assert 8 == len(await redis.keys("stream~*"))
    for stream_name in stream_names:
        assert 6 == (await redis.xlen(stream_name))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 16 == len(run_engine.mock_calls)
    assert (
        mock.call(
            fake_install_id(0),
            "owner-0",
            "repo-0",
            0,
            [
                {"event_type": "pull_request", "data": {"payload": 0}},
                {"event_type": "pull_request", "data": {"payload": 1}},
                {"event_type": "pull_request", "data": {"payload": 2}},
            ],
        )
        in run_engine.mock_calls
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
@mock.patch("mergify_engine.github_events.extract_pull_numbers_from_event")
async def test_worker_expanded_events(
    extract_pull_numbers_from_event,
    run_engine,
    get_install_by_id,
    redis,
    logger_checker,
):
    get_install_by_id.side_effect = fake_install_id
    extract_pull_numbers_from_event.return_value = [123, 456, 789]
    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", None, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == (await redis.xlen("stream~12345"))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 3 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        123,
        [
            {"event_type": "pull_request", "data": {"payload": "whatever"}},
            {"event_type": "comment", "data": {"payload": "foobar"}},
        ],
    )
    assert run_engine.mock_calls[1] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        456,
        [{"event_type": "comment", "data": {"payload": "foobar"}},],
    )
    assert run_engine.mock_calls[2] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        789,
        [{"event_type": "comment", "data": {"payload": "foobar"}},],
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_one_task(
    run_engine, get_install_by_id, redis, logger_checker
):
    get_install_by_id.side_effect = fake_install_id
    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", 123, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == (await redis.xlen("stream~12345"))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 1 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        123,
        [
            {"event_type": "pull_request", "data": {"payload": "whatever"}},
            {"event_type": "comment", "data": {"payload": "foobar"}},
        ],
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_unexisting_stream(
    run_engine, get_install_by_id, redis, logger_checker
):
    get_install_by_id.side_effect = fake_install_id
    p = worker.StreamProcessor(redis)
    await p.consume("stream~666")
    assert len(run_engine.mock_calls) == 0


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_good_stream(
    run_engine, get_install_by_id, redis, logger_checker
):
    get_install_by_id.side_effect = fake_install_id

    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", 123, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == await redis.xlen("stream~12345")
    assert 0 == len(await redis.hgetall("attempts"))

    p = worker.StreamProcessor(redis)
    await p.consume("stream~12345")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        123,
        [
            {"event_type": "pull_request", "data": {"payload": "whatever"}},
            {"event_type": "comment", "data": {"payload": "foobar"}},
        ],
    )

    # Check redis is empty
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.logs.getLogger")
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_pull(
    run_engine, get_install_by_id, logger_class, redis
):
    get_install_by_id.side_effect = fake_install_id
    logs.setup_logging(worker="streams")
    logger = logger_class.return_value

    # One retries once, the other reaches max_retry
    run_engine.side_effect = [
        exceptions.MergeableStateUnknown(mock.Mock()),
        exceptions.MergeableStateUnknown(mock.Mock()),
        mock.Mock(),
        exceptions.MergeableStateUnknown(mock.Mock()),
        exceptions.MergeableStateUnknown(mock.Mock()),
    ]

    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", 42, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == await redis.xlen("stream~12345")
    assert 0 == len(await redis.hgetall("attempts"))

    p = worker.StreamProcessor(redis)
    await p.consume("stream~12345")

    assert len(run_engine.mock_calls) == 2
    assert run_engine.mock_calls == [
        mock.call(
            fake_install_id(12345),
            "owner",
            "repo",
            123,
            [{"event_type": "pull_request", "data": {"payload": "whatever"}},],
        ),
        mock.call(
            fake_install_id(12345),
            "owner",
            "repo",
            42,
            [{"event_type": "comment", "data": {"payload": "foobar"}},],
        ),
    ]

    # Check stream still there and attempts recorded
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert {
        b"pull~12345~owner~repo~42": b"1",
        b"pull~12345~owner~repo~123": b"1",
    } == await redis.hgetall("attempts")

    await p.consume("stream~12345")
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 1 == len(await redis.hgetall("attempts"))
    assert len(run_engine.mock_calls) == 4
    assert {b"pull~12345~owner~repo~42": b"2"} == await redis.hgetall("attempts")

    await p.consume("stream~12345")
    assert len(run_engine.mock_calls) == 5

    # Too many retries, everything is gone
    assert 3 == len(logger.info.mock_calls)
    assert 1 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == (
        "failed to process pull request, retrying",
    )
    assert logger.info.mock_calls[1].args == (
        "failed to process pull request, retrying",
    )
    assert logger.error.mock_calls[0].args == (
        "failed to process pull request, abandoning",
    )
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_recovered(
    run_engine, get_install_by_id, logger, redis
):
    get_install_by_id.side_effect = fake_install_id
    logs.setup_logging(worker="streams")

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(response=response)

    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", 123, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == await redis.xlen("stream~12345")
    assert 0 == len(await redis.hgetall("attempts"))

    p = worker.StreamProcessor(redis)
    await p.consume("stream~12345")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        123,
        [
            {"event_type": "pull_request", "data": {"payload": "whatever"}},
            {"event_type": "comment", "data": {"payload": "foobar"}},
        ],
    )

    # Check stream still there and attempts recorded
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 1 == len(await redis.hgetall("attempts"))

    assert {b"stream~12345": b"1"} == await redis.hgetall("attempts")

    run_engine.side_effect = None

    await p.consume("stream~12345")
    assert len(run_engine.mock_calls) == 2
    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))

    assert 1 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process stream, retrying",)


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_failure(
    run_engine, get_install_by_id, logger, redis
):
    get_install_by_id.side_effect = fake_install_id
    logs.setup_logging(worker="streams")

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(response=response)

    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )
    await worker.push(
        redis, 12345, "owner", "repo", 123, "comment", {"payload": "foobar"},
    )

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 2 == await redis.xlen("stream~12345")
    assert 0 == len(await redis.hgetall("attempts"))

    p = worker.StreamProcessor(redis)
    await p.consume("stream~12345")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        fake_install_id(12345),
        "owner",
        "repo",
        123,
        [
            {"event_type": "pull_request", "data": {"payload": "whatever"}},
            {"event_type": "comment", "data": {"payload": "foobar"}},
        ],
    )

    # Check stream still there and attempts recorded
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 1 == len(await redis.hgetall("attempts"))

    assert {b"stream~12345": b"1"} == await redis.hgetall("attempts")

    await p.consume("stream~12345")
    assert len(run_engine.mock_calls) == 2
    assert {b"stream~12345": b"2"} == await redis.hgetall("attempts")

    await p.consume("stream~12345")
    assert len(run_engine.mock_calls) == 3

    # Still there
    assert 3 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process stream, retrying",)
    assert logger.info.mock_calls[1].args == ("failed to process stream, retrying",)
    assert logger.info.mock_calls[2].args == ("failed to process stream, retrying",)
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 1 == len(await redis.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.logs.getLogger")
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_pull_unexpected_error(
    run_engine, get_install_by_id, logger_class, redis
):
    get_install_by_id.side_effect = fake_install_id
    logs.setup_logging(worker="streams")
    logger = logger_class.return_value

    run_engine.side_effect = Exception

    await worker.push(
        redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
    )

    p = worker.StreamProcessor(redis)
    await p.consume("stream~12345")
    await p.consume("stream~12345")

    # Exception have been logged, redis must be clean
    assert len(run_engine.mock_calls) == 2
    assert len(logger.error.mock_calls) == 2
    assert logger.error.mock_calls[0].args == ("failed to process pull request",)
    assert logger.error.mock_calls[1].args == ("failed to process pull request",)
    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.clients.github.get_installation_by_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_date_scheduling(
    run_engine, get_install_by_id, redis, logger_checker
):
    get_install_by_id.side_effect = fake_install_id

    # Don't process it before 2040
    with freeze_time("2040-01-01"):
        await worker.push(
            redis, 12345, "owner", "repo", 123, "pull_request", {"payload": "whatever"},
        )
        unwanted_installation_id = fake_install_id(12345)

    with freeze_time("2020-01-01"):
        await worker.push(
            redis, 54321, "owner", "repo", 321, "pull_request", {"payload": "foobar"},
        )
        wanted_installation_id = fake_install_id(54321)

    assert 2 == (await redis.zcard("streams"))
    assert 2 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))

    s = worker.StreamSelector(1, redis)
    p = worker.StreamProcessor(redis)

    received = []

    def fake_engine(installation_id, owner, repo, pull_number, sources):
        received.append(installation_id)

    run_engine.side_effect = fake_engine

    with freeze_time("2020-01-14"):
        async with s.next_stream() as stream_name:
            assert stream_name is not None
            await p.consume(stream_name)

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))
    assert received == [wanted_installation_id]

    with freeze_time("2030-01-14"):
        async with s.next_stream() as stream_name:
            assert stream_name is None

    assert 1 == (await redis.zcard("streams"))
    assert 1 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))
    assert received == [wanted_installation_id]

    # We are in 2041, we have something todo :)
    with freeze_time("2041-01-14"):
        async with s.next_stream() as stream_name:
            assert stream_name is not None
            await p.consume(stream_name)

    assert 0 == (await redis.zcard("streams"))
    assert 0 == len(await redis.keys("stream~*"))
    assert 0 == len(await redis.hgetall("attempts"))
    assert received == [wanted_installation_id, unwanted_installation_id]
