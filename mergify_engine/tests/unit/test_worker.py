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
import dataclasses
import datetime
import json
import time
from unittest import mock

from freezegun import freeze_time
import httpx
import pytest

from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import worker
from mergify_engine.clients import http


async def run_worker(test_timeout=10, **kwargs):
    w = worker.Worker(**kwargs)
    await w.start()
    started_at = time.monotonic()
    while (
        w._redis_stream is None or (await w._redis_stream.zcard("streams")) > 0
    ) and time.monotonic() - started_at < test_timeout:
        await asyncio.sleep(0.5)
    w.stop()
    await w.wait_shutdown_complete()


@dataclasses.dataclass
class InstallationMatcher:
    owner: str

    def __eq__(self, installation: object) -> bool:
        if not isinstance(installation, context.Installation):
            return NotImplemented
        return self.owner == installation.owner_login


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_waiting_tasks(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    stream_names = []
    for installation_id in range(8):
        for pull_number in range(2):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                repo_id = installation_id
                owner_id = installation_id
                stream_names.append(f"stream~owner-{installation_id}~{owner_id}")
                await worker.push(
                    redis_stream,
                    owner_id,
                    owner,
                    repo_id,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    # Check everything we push are in redis
    assert 8 == (await redis_stream.zcard("streams"))
    assert 8 == len(await redis_stream.keys("stream~*"))
    for stream_name in stream_names:
        assert 6 == (await redis_stream.xlen(stream_name))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 16 == len(run_engine.mock_calls)
    assert (
        mock.call(
            InstallationMatcher(owner="owner-0"),
            0,
            "repo-0",
            0,
            [
                {
                    "event_type": "pull_request",
                    "data": {"payload": 0},
                    "timestamp": mock.ANY,
                },
                {
                    "event_type": "pull_request",
                    "data": {"payload": 1},
                    "timestamp": mock.ANY,
                },
                {
                    "event_type": "pull_request",
                    "data": {"payload": 2},
                    "timestamp": mock.ANY,
                },
            ],
        )
        in run_engine.mock_calls
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
@mock.patch("mergify_engine.clients.github.aget_client")
@mock.patch("mergify_engine.github_events.extract_pull_numbers_from_event")
async def test_worker_expanded_events(
    extract_pull_numbers_from_event,
    aget_client,
    run_engine,
    _,
    redis_stream,
    redis_cache,
    logger_checker,
):
    client = mock.Mock(
        name="foo",
        owner="owner",
        repo="repo",
        auth=mock.Mock(
            installation={"id": 12345}, owner="owner", repo="repo", owner_id=123
        ),
    )
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock()
    client.items.return_value = mock.AsyncMock()

    aget_client.return_value = client

    extract_pull_numbers_from_event.return_value = [123, 456, 789]
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        None,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == (await redis_stream.xlen("stream~owner~123"))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 3 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        123,
        [
            {
                "event_type": "pull_request",
                "data": {"payload": "whatever"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )
    assert run_engine.mock_calls[1] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        456,
        [
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )
    assert run_engine.mock_calls[2] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        789,
        [
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_one_task(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == (await redis_stream.xlen("stream~owner~123"))

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 1 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        123,
        [
            {
                "event_type": "pull_request",
                "data": {"payload": "whatever"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_unexisting_stream(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~notexists~2")
    assert len(run_engine.mock_calls) == 0


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_good_stream(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == await redis_stream.xlen("stream~owner~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~owner~123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        123,
        [
            {
                "event_type": "pull_request",
                "data": {"payload": "whatever"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.daiquiri.getLogger")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_pull(
    run_engine, _, logger_class, redis_stream, redis_cache
):
    logs.setup_logging()
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
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        42,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == await redis_stream.xlen("stream~owner~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~owner~123")

    assert len(run_engine.mock_calls) == 2
    assert run_engine.mock_calls == [
        mock.call(
            InstallationMatcher(owner="owner"),
            123,
            "repo",
            123,
            [
                {
                    "event_type": "pull_request",
                    "data": {"payload": "whatever"},
                    "timestamp": mock.ANY,
                },
            ],
        ),
        mock.call(
            InstallationMatcher(owner="owner"),
            123,
            "repo",
            42,
            [
                {
                    "event_type": "comment",
                    "data": {"payload": "foobar"},
                    "timestamp": mock.ANY,
                },
            ],
        ),
    ]

    # Check stream still there and attempts recorded
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert {
        b"pull~owner~repo~42": b"1",
        b"pull~owner~repo~123": b"1",
    } == await redis_stream.hgetall("attempts")

    await p.consume("stream~owner~123")
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 1 == len(await redis_stream.hgetall("attempts"))
    assert len(run_engine.mock_calls) == 4
    assert {b"pull~owner~repo~42": b"2"} == await redis_stream.hgetall("attempts")

    await p.consume("stream~owner~123")
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
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_recovered(
    run_engine, _, logger, redis_stream, redis_cache
):
    logs.setup_logging()

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(
        message="foobar", request=response.request, response=response
    )

    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == await redis_stream.xlen("stream~owner~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~owner~123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        123,
        [
            {
                "event_type": "pull_request",
                "data": {"payload": "whatever"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )

    # Check stream still there and attempts recorded
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 1 == len(await redis_stream.hgetall("attempts"))

    assert {b"stream~owner~123": b"1"} == await redis_stream.hgetall("attempts")

    run_engine.side_effect = None

    await p.consume("stream~owner~123")
    assert len(run_engine.mock_calls) == 2
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    assert 1 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process stream, retrying",)


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_failure(
    run_engine, _, logger, redis_stream, redis_cache
):
    logs.setup_logging()

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(
        message="foobar", request=response.request, response=response
    )

    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 2 == await redis_stream.xlen("stream~owner~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~owner~123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner"),
        123,
        "repo",
        123,
        [
            {
                "event_type": "pull_request",
                "data": {"payload": "whatever"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "comment",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )

    # Check stream still there and attempts recorded
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 1 == len(await redis_stream.hgetall("attempts"))

    assert {b"stream~owner~123": b"1"} == await redis_stream.hgetall("attempts")

    await p.consume("stream~owner~123")
    assert len(run_engine.mock_calls) == 2
    assert {b"stream~owner~123": b"2"} == await redis_stream.hgetall("attempts")

    await p.consume("stream~owner~123")
    assert len(run_engine.mock_calls) == 3

    # Still there
    assert 3 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process stream, retrying",)
    assert logger.info.mock_calls[1].args == ("failed to process stream, retrying",)
    assert logger.info.mock_calls[2].args == ("failed to process stream, retrying",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 1 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.daiquiri.getLogger")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_pull_unexpected_error(
    run_engine, _, logger_class, redis_stream, redis_cache
):
    logs.setup_logging()
    logger = logger_class.return_value

    run_engine.side_effect = Exception

    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    p = worker.StreamProcessor(redis_stream, redis_cache)
    await p.consume("stream~owner~123")
    await p.consume("stream~owner~123")

    # Exception have been logged, redis must be clean
    assert len(run_engine.mock_calls) == 2
    assert len(logger.error.mock_calls) == 2
    assert logger.error.mock_calls[0].args == ("failed to process pull request",)
    assert logger.error.mock_calls[1].args == ("failed to process pull request",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_date_scheduling(
    run_engine, _, redis_stream, redis_cache, logger_checker
):

    # Don't process it before 2040
    with freeze_time("2040-01-01"):
        await worker.push(
            redis_stream,
            123,
            "owner1",
            123,
            "repo",
            123,
            "pull_request",
            {"payload": "whatever"},
        )
        unwanted_owner_id = "owner1"

    with freeze_time("2020-01-01"):
        await worker.push(
            redis_stream,
            123,
            "owner2",
            123,
            "repo",
            321,
            "pull_request",
            {"payload": "foobar"},
        )
        wanted_owner_id = "owner2"

    assert 2 == (await redis_stream.zcard("streams"))
    assert 2 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    s = worker.StreamSelector(redis_stream, 0, 1)
    p = worker.StreamProcessor(redis_stream, redis_cache)

    received = []

    def fake_engine(installation, repo_id, repo, pull_number, sources):
        received.append(installation.owner_login)

    run_engine.side_effect = fake_engine

    with freeze_time("2020-01-14"):
        stream_name = await s.next_stream()
        assert stream_name is not None
        await p.consume(stream_name)

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    with freeze_time("2030-01-14"):
        stream_name = await s.next_stream()
        assert stream_name is None

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    # We are in 2041, we have something todo :)
    with freeze_time("2041-01-14"):
        stream_name = await s.next_stream()
        assert stream_name is not None
        await p.consume(stream_name)

    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id, unwanted_owner_id]


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
async def test_worker_debug_report(_, redis_stream, redis_cache, logger_checker):
    for installation_id in range(8):
        for pull_number in range(2):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                await worker.push(
                    redis_stream,
                    123,
                    owner,
                    123,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    await worker.async_status()


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_after_read_error(
    run_engine, _, redis_stream, redis_cache
):
    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 503
    run_engine.side_effect = httpx.ReadError(
        "Server disconnected while attempting read",
        request=mock.Mock(),
    )

    p = worker.StreamProcessor(redis_stream, redis_cache)

    installation = context.Installation(123, "owner", {}, None, None)
    with pytest.raises(worker.StreamRetry):
        async with p._translate_exception_to_retries(
            worker.StreamNameType("stream~owner~123")
        ):
            await worker.run_engine(installation, 123, "repo", 1234, [])


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_ignore_503(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    response = mock.Mock()
    response.text = "Server Error: Sorry, this diff is taking too long to generate."
    response.status_code = 503
    response.json.side_effect = json.JSONDecodeError("whatever", "", 0)
    response.reason_phrase = "Service Unavailable"
    response.url = "https://api.github.com/repositories/1234/pulls/5/files"

    run_engine.side_effect = lambda *_: http.raise_for_status(response)

    await worker.push(
        redis_stream,
        123,
        "owner1",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_multiple_workers(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    stream_names = []
    for installation_id in range(100):
        for pull_number in range(2):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                repo_id = installation_id
                owner_id = installation_id
                stream_names.append(f"stream~owner-{installation_id}~{owner_id}")
                await worker.push(
                    redis_stream,
                    owner_id,
                    owner,
                    repo_id,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    # Check everything we push are in redis
    assert 100 == (await redis_stream.zcard("streams"))
    assert 100 == len(await redis_stream.keys("stream~*"))
    for stream_name in stream_names:
        assert 6 == (await redis_stream.xlen(stream_name))

    process_count = 4
    worker_per_process = 3

    await asyncio.gather(
        *[
            run_worker(
                worker_per_process=worker_per_process,
                process_count=process_count,
                process_index=i,
            )
            for i in range(process_count)
        ]
    )

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 200 == len(run_engine.mock_calls)


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_reschedule(
    run_engine, _, redis_stream, redis_cache, logger_checker, monkeypatch
):
    monkeypatch.setattr("mergify_engine.worker.WORKER_PROCESSING_DELAY", 3000)
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    score = (await redis_stream.zrange("streams", 0, -1, withscores=True))[0][1]
    planned_for = datetime.datetime.utcfromtimestamp(score)

    monkeypatch.setattr("sys.argv", ["mergify-worker-rescheduler", "other"])
    ret = await worker.async_reschedule_now()
    assert ret == 1

    score_not_rescheduled = (
        await redis_stream.zrange("streams", 0, -1, withscores=True)
    )[0][1]
    planned_for_not_rescheduled = datetime.datetime.utcfromtimestamp(
        score_not_rescheduled
    )
    assert planned_for == planned_for_not_rescheduled

    monkeypatch.setattr("sys.argv", ["mergify-worker-rescheduler", "OwNer"])
    ret = await worker.async_reschedule_now()
    assert ret == 0

    score_rescheduled = (await redis_stream.zrange("streams", 0, -1, withscores=True))[
        0
    ][1]
    planned_for_rescheduled = datetime.datetime.utcfromtimestamp(score_rescheduled)
    assert planned_for > planned_for_rescheduled


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_stuck_shutdown(
    run_engine, _, redis_stream, redis_cache, logger_checker
):
    async def fake_engine(*args, **kwargs):
        await asyncio.sleep(10000000)

    run_engine.side_effect = fake_engine
    await worker.push(
        redis_stream,
        123,
        "owner",
        123,
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await run_worker(test_timeout=2, shutdown_timeout=1)
