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
from mergify_engine import date
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import logs
from mergify_engine import worker
from mergify_engine import worker_lua
from mergify_engine.clients import github_app
from mergify_engine.clients import http


async def fake_get_subscription(*args, **kwargs):
    sub = mock.Mock()
    sub.has_feature.return_value = False
    return sub


FAKE_INSTALLATION = {
    "id": 12345,
    "account": {"id": 123, "login": "owner-123"},
    "target_type": "Organization",
    "permissions": github_app.EXPECTED_MINIMAL_PERMISSIONS["Organization"],
}


def fake_get_installation_from_account_id(
    owner_id: github_types.GitHubAccountIdType,
) -> github_types.GitHubInstallation:
    return {
        "id": github_types.GitHubInstallationIdType(12345),
        "account": {
            "id": owner_id,
            "login": github_types.GitHubLogin(f"owner-{owner_id}"),
            "type": "User",
            "avatar_url": "",
        },
        "target_type": "Organization",
        "permissions": github_app.EXPECTED_MINIMAL_PERMISSIONS["Organization"],
    }


async def run_worker(test_timeout=10, **kwargs):
    w = worker.Worker(
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
        **kwargs,
    )
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
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_waiting_tasks(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
    buckets = set()
    bucket_sources = set()
    for installation_id in range(8):
        for pull_number in range(1, 3):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                repo_id = installation_id
                owner_id = installation_id
                buckets.add(f"bucket~{owner_id}~{owner}")
                bucket_sources.add(f"bucket-sources~{repo_id}~{repo}~{pull_number}")
                await worker.push(
                    redis_stream,
                    owner_id,
                    owner,
                    repo_id,
                    repo,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    # Check everything we push are in redis
    assert 8 == (await redis_stream.zcard("streams"))
    assert 8 == len(await redis_stream.keys("bucket~*"))
    assert 16 == len(await redis_stream.keys("bucket-sources~*"))
    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 16 == len(run_engine.mock_calls)
    assert (
        mock.call(
            InstallationMatcher(owner="owner-0"),
            0,
            "repo-0",
            1,
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
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.clients.github.aget_client")
@mock.patch("mergify_engine.github_events.extract_pull_numbers_from_event")
async def test_worker_expanded_events(
    extract_pull_numbers_from_event,
    aget_client,
    get_installation_from_account_id,
    run_engine,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
    client = mock.Mock(
        auth=mock.Mock(
            owner_id=123,
            owner_login="owner-123",
            installation=fake_get_installation_from_account_id(123),
        ),
    )
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock()
    client.items.return_value = mock.AsyncMock()

    aget_client.return_value = client

    extract_pull_numbers_from_event.side_effect = [[123, 456, 789], [123, 789]]
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        None,
        "push",
        {"payload": "foobar"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        None,
        "check_run",
        {"payload": "foobar"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    assert 1 == await redis_stream.zcard("streams")
    assert 2 == await redis_stream.zcard("bucket~123~owner-123")
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~0")
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~123")

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data, order is very important
    # push event on pull request with other events must go last
    assert 3 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner-123"),
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
                "event_type": "push",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "check_run",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )
    assert run_engine.mock_calls[1] == mock.call(
        InstallationMatcher(owner="owner-123"),
        123,
        "repo",
        789,
        [
            {
                "event_type": "push",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
            {
                "event_type": "check_run",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )
    assert run_engine.mock_calls[2] == mock.call(
        InstallationMatcher(owner="owner-123"),
        123,
        "repo",
        456,
        [
            {
                "event_type": "push",
                "data": {"payload": "foobar"},
                "timestamp": mock.ANY,
            },
        ],
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_one_task(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_subscription.side_effect = fake_get_subscription
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 1 == len(run_engine.mock_calls)
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner-123"),
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
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_unexisting_stream(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_subscription.side_effect = fake_get_subscription
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("buckets~2~notexists", 2, "notexists")
    assert len(run_engine.mock_calls) == 0


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_consume_good_stream(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_subscription.side_effect = fake_get_subscription
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")

    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("bucket~123~owner-123", 123, "owner-123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner-123"),
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
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.daiquiri.getLogger")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_pull(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    logger_class,
    redis_stream,
    redis_cache,
):
    logs.setup_logging()
    logger = logger_class.return_value

    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
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
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        42,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~42")

    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("bucket~123~owner-123", 123, "owner-123")

    assert len(run_engine.mock_calls) == 2
    assert run_engine.mock_calls == [
        mock.call(
            InstallationMatcher(owner="owner-123"),
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
            InstallationMatcher(owner="owner-123"),
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
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~42")
    assert {
        b"bucket-sources~123~repo~42": b"1",
        b"bucket-sources~123~repo~123": b"1",
    } == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123~owner-123", 123, "owner-123")
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~repo~42")

    assert 1 == len(await redis_stream.hgetall("attempts"))
    assert len(run_engine.mock_calls) == 4
    assert {b"bucket-sources~123~repo~42": b"2"} == await redis_stream.hgetall(
        "attempts"
    )

    await p.consume("bucket~123~owner-123", 123, "owner-123")
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
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == await redis_stream.zcard("bucket~123~owner-123")
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_recovered(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    logger,
    redis_stream,
    redis_cache,
):
    logs.setup_logging()

    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(
        message="foobar", request=response.request, response=response
    )

    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("bucket~123~owner-123", 123, "owner-123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner-123"),
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
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 1 == len(await redis_stream.hgetall("attempts"))

    assert {b"bucket~123~owner-123": b"1"} == await redis_stream.hgetall("attempts")

    run_engine.side_effect = None

    await p.consume("bucket~123~owner-123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 2
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == await redis_stream.zcard("bucket~123~owner-123")
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))

    assert 1 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process org bucket, retrying",)


@pytest.mark.asyncio
@mock.patch.object(worker, "LOG")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_stream_failure(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    logger,
    redis_stream,
    redis_cache,
):
    logs.setup_logging()

    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 401
    run_engine.side_effect = http.HTTPClientSideError(
        message="foobar", request=response.request, response=response
    )

    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == await redis_stream.zcard("streams")
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("bucket~123~owner-123", 123, "owner-123")

    assert len(run_engine.mock_calls) == 1
    assert run_engine.mock_calls[0] == mock.call(
        InstallationMatcher(owner="owner-123"),
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
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == len(await redis_stream.hgetall("attempts"))

    assert {b"bucket~123~owner-123": b"1"} == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123~owner-123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 2
    assert {b"bucket~123~owner-123": b"2"} == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123~owner-123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 3

    # Still there
    assert 3 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process org bucket, retrying",)
    assert logger.info.mock_calls[1].args == ("failed to process org bucket, retrying",)
    assert logger.info.mock_calls[2].args == ("failed to process org bucket, retrying",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123~owner-123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~repo~123")
    assert 1 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.daiquiri.getLogger")
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_pull_unexpected_error(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    logger_class,
    redis_stream,
    redis_cache,
):
    logs.setup_logging()
    logger = logger_class.return_value

    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
    run_engine.side_effect = Exception

    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    p = worker.StreamProcessor(redis_stream, redis_cache, False)
    await p.consume("bucket~123~owner-123", 123, "owner-123")
    await p.consume("bucket~123~owner-123", 123, "owner-123")

    # Exception have been logged, redis must be clean
    assert len(run_engine.mock_calls) == 2
    assert len(logger.error.mock_calls) == 2
    assert logger.error.mock_calls[0].args == ("failed to process pull request",)
    assert logger.error.mock_calls[1].args == ("failed to process pull request",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_date_scheduling(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):

    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription
    # Don't process it before 2040
    with freeze_time("2040-01-01"):
        await worker.push(
            redis_stream,
            123,
            "owner-123",
            123,
            "repo",
            "repo",
            123,
            "pull_request",
            {"payload": "whatever"},
        )
        unwanted_owner_id = "owner-123"

    with freeze_time("2020-01-01"):
        await worker.push(
            redis_stream,
            234,
            "owner-234",
            123,
            "repo",
            "repo",
            321,
            "pull_request",
            {"payload": "foobar"},
        )
        wanted_owner_id = "owner-234"

    assert 2 == (await redis_stream.zcard("streams"))
    assert 2 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    s = worker.SharedOrgBucketSelector(redis_stream, 0, 1)
    p = worker.StreamProcessor(redis_stream, redis_cache, False)

    received = []

    def fake_engine(installation, repo_id, repo, pull_number, sources):
        received.append(installation.owner_login)

    run_engine.side_effect = fake_engine

    with freeze_time("2020-01-14"):
        stream_name = await s.next_org_bucket()
        assert stream_name is not None
        owner_id = worker.Worker.extract_owner(stream_name)
        await p.consume(stream_name, owner_id, f"owner-{owner_id}")

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    with freeze_time("2030-01-14"):
        stream_name = await s.next_org_bucket()
        assert stream_name is None

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    # We are in 2041, we have something todo :)
    with freeze_time("2041-01-14"):
        stream_name = await s.next_org_bucket()
        assert stream_name is not None
        owner_id = worker.Worker.extract_owner(stream_name)
        await p.consume(stream_name, owner_id, f"owner-{owner_id}")

    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id, unwanted_owner_id]


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
async def test_worker_drop_bucket(
    get_subscription, redis_stream, redis_cache, logger_checker
):
    get_subscription.side_effect = fake_get_subscription

    buckets = set()
    bucket_sources = set()
    _id = 123
    for pull_number in range(2):
        for data in range(3):
            owner = f"owner-{_id}"
            repo = f"repo-{_id}"
            repo_id = _id
            owner_id = _id
            buckets.add(f"bucket~{owner_id}~{owner}")
            bucket_sources.add(f"bucket-sources~{repo_id}~{repo}~{pull_number}")
            await worker.push(
                redis_stream,
                owner_id,
                owner,
                repo_id,
                repo,
                repo,
                pull_number,
                "pull_request",
                {"payload": data},
            )

    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))

    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

    await worker_lua.drop_bucket(redis_stream, "bucket~123~owner-123")
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))

    await worker_lua.clean_org_bucket(
        redis_stream, "bucket~123~owner-123", date.utcnow()
    )

    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
async def test_worker_debug_report(
    get_subscription, redis_stream, redis_cache, logger_checker
):
    get_subscription.side_effect = fake_get_subscription

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
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    await worker.async_status()


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_retrying_after_read_error(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

    response = mock.Mock()
    response.json.return_value = {"message": "boom"}
    response.status_code = 503
    run_engine.side_effect = httpx.ReadError(
        "Server disconnected while attempting read",
        request=mock.Mock(),
    )

    p = worker.StreamProcessor(redis_stream, redis_cache, False)

    installation = context.Installation(FAKE_INSTALLATION, {}, None, None)
    with pytest.raises(worker.OrgBucketRetry):
        async with p._translate_exception_to_retries(
            worker_lua.BucketOrgKeyType("stream~owner~123")
        ):
            await worker.run_engine(installation, 123, "repo", 1234, [])


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_stream_processor_ignore_503(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

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
        "owner-123",
        123,
        "repo",
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
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_with_multiple_workers(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

    buckets = set()
    bucket_sources = set()
    for installation_id in range(100):
        for pull_number in range(1, 3):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                repo_id = installation_id
                owner_id = installation_id
                buckets.add(f"bucket~{owner_id}~{owner}")
                bucket_sources.add(f"bucket-sources~{repo_id}~{repo}~{pull_number}")
                await worker.push(
                    redis_stream,
                    owner_id,
                    owner,
                    repo_id,
                    repo,
                    repo,
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    # Check everything we push are in redis
    assert 100 == (await redis_stream.zcard("streams"))
    assert 100 == len(await redis_stream.keys("bucket~*"))
    assert 200 == len(await redis_stream.keys("bucket-sources~*"))
    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

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
    assert 0 == len(await redis_stream.keys("buckets~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 200 == len(run_engine.mock_calls)


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_reschedule(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
    monkeypatch,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

    monkeypatch.setattr("mergify_engine.worker.WORKER_PROCESSING_DELAY", 3000)
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
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

    monkeypatch.setattr("sys.argv", ["mergify-worker-rescheduler", "123"])
    ret = await worker.async_reschedule_now()
    assert ret == 0

    score_rescheduled = (await redis_stream.zrange("streams", 0, -1, withscores=True))[
        0
    ][1]
    planned_for_rescheduled = datetime.datetime.utcfromtimestamp(score_rescheduled)
    assert planned_for > planned_for_rescheduled


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_stuck_shutdown(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id
    get_subscription.side_effect = fake_get_subscription

    async def fake_engine(*args, **kwargs):
        await asyncio.sleep(10000000)

    run_engine.side_effect = fake_engine
    await worker.push(
        redis_stream,
        123,
        "owner-123",
        123,
        "repo",
        "repo",
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await run_worker(test_timeout=2, shutdown_timeout=1)


@pytest.mark.asyncio
@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_dedicated_worker_scaleup_scaledown(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id

    w = worker.Worker(
        worker_per_process=3,
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
    )
    await w.start()

    tracker = []

    async def track_context(*args, **kwargs):
        tracker.append(logs.WORKER_ID.get(None))

    run_engine.side_effect = track_context

    async def fake_get_subscription_dedicated(redis, owner_id):
        sub = mock.Mock()
        # 1 is always shared
        if owner_id == 1:
            sub.has_feature.return_value = False
        else:
            sub.has_feature.return_value = True
        return sub

    async def fake_get_subscription_shared(redis, owner_id):
        sub = mock.Mock()
        sub.has_feature.return_value = False
        return sub

    async def push_and_wait():
        # worker hash == 2
        await worker.push(
            redis_stream,
            4242,
            "owner-4242",
            4242,
            "repo",
            "repo",
            4242,
            "pull_request",
            {"payload": "whatever"},
        )
        # worker hash == 1
        await worker.push(
            redis_stream,
            1,
            "owner-1",
            1,
            "repo",
            "repo",
            1,
            "pull_request",
            {"payload": "whatever"},
        )
        # worker hash == 0
        await worker.push(
            redis_stream,
            120,
            "owner-120",
            120,
            "repo",
            "repo",
            120,
            "pull_request",
            {"payload": "whatever"},
        )
        started_at = time.monotonic()
        while (
            w._redis_stream is None or (await w._redis_stream.zcard("streams")) > 0
        ) and time.monotonic() - started_at < 10:
            await asyncio.sleep(0.5)

    get_subscription.side_effect = fake_get_subscription_dedicated
    await push_and_wait()
    assert sorted(tracker) == [
        "dedicated-120",
        "dedicated-4242",
        "shared-1",
    ]
    tracker.clear()

    get_subscription.side_effect = fake_get_subscription_shared
    await push_and_wait()
    await push_and_wait()
    assert sorted(tracker) == [
        "shared-0",
        "shared-0",
        "shared-1",
        "shared-1",
        "shared-2",
        "shared-2",
    ]
    tracker.clear()

    get_subscription.side_effect = fake_get_subscription_dedicated
    await push_and_wait()
    await push_and_wait()
    assert sorted(tracker) == [
        "dedicated-120",
        "dedicated-120",
        "dedicated-4242",
        "dedicated-4242",
        "shared-1",
        "shared-1",
    ]
    tracker.clear()

    get_subscription.side_effect = fake_get_subscription_shared
    await push_and_wait()
    assert sorted(tracker) == [
        "shared-0",
        "shared-1",
        "shared-2",
    ]

    w.stop()
    await w.wait_shutdown_complete()
