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
import typing
from unittest import mock

from freezegun import freeze_time
import httpx
import msgpack
import pytest

from mergify_engine import context
from mergify_engine import date
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine import worker_lua
from mergify_engine.clients import github_app
from mergify_engine.clients import http


# NOTE(sileht): old version of the worker.push() method (3.0.0)
# Since we do rolling upgrade of the API and worker.push is used by the API
# We can't migrate the data on the worker startup.
# So we need to support at least until next major version the old format in Redis database
async def legacy_push(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
    repo_id: github_types.GitHubRepositoryIdType,
    repo_name: github_types.GitHubRepositoryName,
    tracing_repo_name: github_types.GitHubRepositoryNameForTracing,
    pull_number: typing.Optional[github_types.GitHubPullRequestNumber],
    event_type: github_types.GitHubEventType,
    data: github_types.GitHubEvent,
    score: typing.Optional[str] = None,
) -> None:
    now = date.utcnow()
    event = msgpack.packb(
        {
            "event_type": event_type,
            "data": data,
            "timestamp": now.isoformat(),
        },
        use_bin_type=True,
    )
    scheduled_at = now + datetime.timedelta(seconds=worker.WORKER_PROCESSING_DELAY)
    if score is None:
        score = str(date.utcnow().timestamp())
    bucket_org_key = worker_lua.BucketOrgKeyType(f"bucket~{owner_id}~{owner_login}")
    bucket_sources_key = worker_lua.BucketSourcesKeyType(
        f"bucket-sources~{repo_id}~{repo_name}~{pull_number or 0}"
    )
    await worker_lua.push_pull(
        redis,
        bucket_org_key,
        bucket_sources_key,
        tracing_repo_name,
        scheduled_at,
        event,
        score,
    )


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


async def run_worker(test_timeout: float = 10, **kwargs: typing.Any) -> worker.Worker:
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
    return w


@dataclasses.dataclass
class InstallationMatcher:
    owner: str

    def __eq__(self, installation: object) -> bool:
        if not isinstance(installation, context.Installation):
            return NotImplemented
        return self.owner == installation.owner_login


@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_worker_legacy_push(
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
                await legacy_push(
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

    # Push some with the new format too
    for installation_id in range(8):
        for pull_number in range(1, 3):
            for data in range(3):
                owner = f"owner-{installation_id}"
                repo = f"repo-{installation_id}"
                repo_id = installation_id
                owner_id = installation_id
                buckets.add(f"bucket~{owner_id}")
                bucket_sources.add(f"bucket-sources~{repo_id}~{pull_number}")
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
    assert 16 == (await redis_stream.zcard("streams"))
    assert 16 == len(await redis_stream.keys("bucket~*"))
    assert 32 == len(await redis_stream.keys("bucket-sources~*"))
    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

    w = await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 32 == len(run_engine.mock_calls)
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

    assert w._owners_cache._mapping == {i: f"owner-{i}" for i in range(0, 8)}


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
                buckets.add(f"bucket~{owner_id}")
                bucket_sources.add(f"bucket-sources~{repo_id}~{pull_number}")
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
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    assert 1 == await redis_stream.zcard("streams")
    assert 2 == await redis_stream.zcard("bucket~123")
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~0")
    assert 1 == await redis_stream.xlen("bucket-sources~123~123")

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
        123,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")

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
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, worker.OwnerLoginsCache()
    )
    await p.consume("buckets~2~notexists", 2, "notexists")
    assert len(run_engine.mock_calls) == 0


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
        123,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, owners_cache
    )
    await p.consume("bucket~123", 123, "owner-123")
    assert owners_cache.get(123) == "owner-123"

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
        42,
        "comment",
        {"payload": "foobar"},
    )

    # Check everything we push are in redis
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == await redis_stream.zcard("bucket~123")
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))
    assert 1 == await redis_stream.xlen("bucket-sources~123~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~42")

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, owners_cache
    )
    await p.consume("bucket~123", 123, "owner-123")
    assert owners_cache.get(123) == "owner-123"

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
    assert 2 == await redis_stream.zcard("bucket~123")
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))
    assert 1 == await redis_stream.xlen("bucket-sources~123~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~42")
    assert {
        b"bucket-sources~123~42": b"1",
        b"bucket-sources~123~123": b"1",
    } == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123", 123, "owner-123")
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 1 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == await redis_stream.xlen("bucket-sources~123~123")
    assert 1 == await redis_stream.xlen("bucket-sources~123~42")

    assert 1 == len(await redis_stream.hgetall("attempts"))
    assert len(run_engine.mock_calls) == 4
    assert {b"bucket-sources~123~42": b"2"} == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123", 123, "owner-123")
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
    assert 0 == await redis_stream.zcard("bucket~123")
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


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
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, owners_cache
    )
    await p.consume("bucket~123", 123, "owner-123")
    assert owners_cache.get(123) == "owner-123"

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
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")
    assert 1 == len(await redis_stream.hgetall("attempts"))

    assert {b"bucket~123": b"1"} == await redis_stream.hgetall("attempts")

    run_engine.side_effect = None

    await p.consume("bucket~123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 2
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == await redis_stream.zcard("bucket~123")
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))

    assert 1 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process org bucket, retrying",)


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
        123,
        "comment",
        {"payload": "foobar"},
    )

    assert 1 == await redis_stream.zcard("streams")
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")
    assert 0 == len(await redis_stream.hgetall("attempts"))

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, owners_cache
    )
    await p.consume("bucket~123", 123, "owner-123")
    assert owners_cache.get(123) == "owner-123"

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

    assert {b"bucket~123": b"1"} == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 2
    assert {b"bucket~123": b"2"} == await redis_stream.hgetall("attempts")

    await p.consume("bucket~123", 123, "owner-123")
    assert len(run_engine.mock_calls) == 3

    # Still there
    assert 3 == len(logger.info.mock_calls)
    assert 0 == len(logger.error.mock_calls)
    assert logger.info.mock_calls[0].args == ("failed to process org bucket, retrying",)
    assert logger.info.mock_calls[1].args == ("failed to process org bucket, retrying",)
    assert logger.info.mock_calls[2].args == ("failed to process org bucket, retrying",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 1 == await redis_stream.zcard("bucket~123")
    assert 2 == await redis_stream.xlen("bucket-sources~123~123")
    assert 1 == len(await redis_stream.hgetall("attempts"))


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
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, owners_cache
    )
    await p.consume("bucket~123", 123, "owner-123")
    await p.consume("bucket~123", 123, "owner-123")
    assert owners_cache.get(123) == "owner-123"

    # Exception have been logged, redis must be clean
    assert len(run_engine.mock_calls) == 2
    assert len(logger.error.mock_calls) == 2
    assert logger.error.mock_calls[0].args == ("failed to process pull request",)
    assert logger.error.mock_calls[1].args == ("failed to process pull request",)
    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


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
            321,
            "pull_request",
            {"payload": "foobar"},
        )
        wanted_owner_id = "owner-234"

    assert 2 == (await redis_stream.zcard("streams"))
    assert 2 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    owners_cache = worker.OwnerLoginsCache()
    w = worker.Worker(
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
        shared_stream_tasks_per_process=1,
        shared_stream_processes=1,
        process_index=0,
    )
    w._redis_cache = redis_cache
    w._redis_stream = redis_stream

    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-0", None, owners_cache
    )

    received = []

    def fake_engine(installation, repo_id, repo, pull_number, sources):
        received.append(installation.owner_login)

    run_engine.side_effect = fake_engine

    with freeze_time("2020-01-14"):
        await w._stream_worker_task(p)

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    with freeze_time("2030-01-14"):
        await w._stream_worker_task(p)

    assert 1 == (await redis_stream.zcard("streams"))
    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id]

    # We are in 2041, we have something todo :)
    with freeze_time("2041-01-14"):
        await w._stream_worker_task(p)

    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))
    assert received == [wanted_owner_id, unwanted_owner_id]

    assert owners_cache.get(123) == "owner-123"
    assert owners_cache.get(234) == "owner-234"


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
            buckets.add(f"bucket~{owner_id}")
            bucket_sources.add(f"bucket-sources~{repo_id}~{pull_number}")
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

    assert 1 == len(await redis_stream.keys("bucket~*"))
    assert 2 == len(await redis_stream.keys("bucket-sources~*"))

    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

    await worker_lua.drop_bucket(redis_stream, "bucket~123")
    assert 0 == len(await redis_stream.keys("bucket~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))

    await worker_lua.clean_org_bucket(redis_stream, "bucket~123", date.utcnow())

    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


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
                    pull_number,
                    "pull_request",
                    {"payload": data},
                )

    await worker.async_status()


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

    owners_cache = worker.OwnerLoginsCache()
    p = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-0", None, owners_cache
    )

    installation = context.Installation(FAKE_INSTALLATION, {}, None, None)
    with pytest.raises(worker.OrgBucketRetry):
        async with p._translate_exception_to_retries(
            worker_lua.BucketOrgKeyType("stream~owner~123")
        ):
            await worker.run_engine(installation, 123, "repo", 1234, [])

    assert owners_cache.get(123) == "<unknown 123>"


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
        123,
        "pull_request",
        {"payload": "whatever"},
    )

    await run_worker()

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("stream~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))


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
                buckets.add(f"bucket~{owner_id}")
                bucket_sources.add(f"bucket-sources~{repo_id}~{pull_number}")
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
    assert 100 == len(await redis_stream.keys("bucket~*"))
    assert 200 == len(await redis_stream.keys("bucket-sources~*"))
    for bucket in buckets:
        assert 2 == await redis_stream.zcard(bucket)
    for bucket_source in bucket_sources:
        assert 3 == await redis_stream.xlen(bucket_source)

    shared_stream_processes = 4
    shared_stream_tasks_per_process = 3

    await asyncio.gather(
        *[
            run_worker(
                shared_stream_tasks_per_process=shared_stream_tasks_per_process,
                shared_stream_processes=shared_stream_processes,
                process_index=i,
            )
            for i in range(shared_stream_processes)
        ]
    )

    # Check redis is empty
    assert 0 == (await redis_stream.zcard("streams"))
    assert 0 == len(await redis_stream.keys("buckets~*"))
    assert 0 == len(await redis_stream.keys("bucket-sources~*"))
    assert 0 == len(await redis_stream.hgetall("attempts"))

    # Check engine have been run with expect data
    assert 200 == len(run_engine.mock_calls)


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
        123,
        "pull_request",
        {"payload": "whatever"},
    )
    await run_worker(test_timeout=2, shutdown_timeout=1)


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
        shared_stream_tasks_per_process=3,
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
        dedicated_workers_syncer_idle_time=0.01,
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


@mock.patch("mergify_engine.worker.subscription.Subscription.get_subscription")
@mock.patch("mergify_engine.clients.github.get_installation_from_account_id")
@mock.patch("mergify_engine.worker.run_engine")
async def test_separate_dedicated_worker(
    run_engine,
    get_installation_from_account_id,
    get_subscription,
    redis_stream,
    redis_cache,
    logger_checker,
):
    get_installation_from_account_id.side_effect = fake_get_installation_from_account_id

    shared_w = worker.Worker(
        shared_stream_tasks_per_process=3,
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
        enabled_services=("shared-stream",),
    )
    await shared_w.start()

    dedicated_w = worker.Worker(
        shared_stream_tasks_per_process=3,
        delayed_refresh_idle_time=0.01,
        dedicated_workers_spawner_idle_time=0.01,
        enabled_services=("dedicated-stream",),
    )

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

    async def push_and_wait(blocked_stream=0):
        # worker hash == 2
        await worker.push(
            redis_stream,
            4242,
            "owner-4242",
            4242,
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
            120,
            "pull_request",
            {"payload": "whatever"},
        )
        started_at = time.monotonic()
        while (
            redis_stream is None
            or (await redis_stream.zcard("streams")) > blocked_stream
        ) and time.monotonic() - started_at < 10:
            await asyncio.sleep(0.5)

    get_subscription.side_effect = fake_get_subscription_dedicated

    # Start only shared worker
    await push_and_wait(2)
    # only shared is consumed
    assert sorted(tracker) == ["shared-1"]
    tracker.clear()

    shared_w.stop()
    await shared_w.wait_shutdown_complete()
    await dedicated_w.start()
    await push_and_wait(1)
    # only dedicated are consumed
    assert sorted(tracker) == ["dedicated-120", "dedicated-4242"]
    tracker.clear()

    # Start both
    await shared_w.start()
    await push_and_wait()
    assert sorted(tracker) == ["dedicated-120", "dedicated-4242", "shared-1"]
    tracker.clear()

    shared_w.stop()
    await shared_w.wait_shutdown_complete()
    dedicated_w.stop()
    await dedicated_w.wait_shutdown_complete()


@mock.patch("mergify_engine.worker.Worker.setup_signals")
@mock.patch("mergify_engine.worker.Worker.delayed_refresh_task")
@mock.patch("mergify_engine.worker.Worker.monitoring_task")
@mock.patch("mergify_engine.worker.Worker.dedicated_workers_spawner_task")
@mock.patch("mergify_engine.worker.Worker.shared_stream_worker_task")
@mock.patch("mergify_engine.worker.Worker.wait_shutdown_complete")
@mock.patch("mergify_engine.worker.Worker.loop_and_sleep_forever")
def test_worker_start_all_tasks(
    loop_and_sleep_forever: mock.Mock,
    wait_shutdown_complete: mock.Mock,
    shared_stream_worker_task: mock.Mock,
    dedicated_workers_spawner_task: mock.Mock,
    monitoring_task: mock.Mock,
    delayed_refresh_task: mock.Mock,
    setup_signals: mock.Mock,
) -> None:
    async def just_run_once(name, idle_time, func):
        await func()

    loop_and_sleep_forever.side_effect = just_run_once

    worker.main([])
    while not wait_shutdown_complete.called:
        time.sleep(0.01)
    assert shared_stream_worker_task.called
    assert dedicated_workers_spawner_task.called
    assert monitoring_task.called
    assert delayed_refresh_task.called


@mock.patch("mergify_engine.worker.Worker.setup_signals")
@mock.patch("mergify_engine.worker.Worker.delayed_refresh_task")
@mock.patch("mergify_engine.worker.Worker.monitoring_task")
@mock.patch("mergify_engine.worker.Worker.dedicated_workers_spawner_task")
@mock.patch("mergify_engine.worker.Worker.shared_stream_worker_task")
@mock.patch("mergify_engine.worker.Worker.wait_shutdown_complete")
@mock.patch("mergify_engine.worker.Worker.loop_and_sleep_forever")
def test_worker_start_just_shared(
    loop_and_sleep_forever: mock.Mock,
    wait_shutdown_complete: mock.Mock,
    shared_stream_worker_task: mock.Mock,
    dedicated_workers_spawner_task: mock.Mock,
    monitoring_task: mock.Mock,
    delayed_refresh_task: mock.Mock,
    setup_signals: mock.Mock,
) -> None:
    async def just_run_once(name, idle_time, func):
        await func()

    loop_and_sleep_forever.side_effect = just_run_once

    worker.main(["--enabled-services=shared-stream"])
    while not wait_shutdown_complete.called:
        time.sleep(0.01)
    assert shared_stream_worker_task.called
    assert not dedicated_workers_spawner_task.called
    assert not monitoring_task.called
    assert not delayed_refresh_task.called


@mock.patch("mergify_engine.worker.Worker.setup_signals")
@mock.patch("mergify_engine.worker.Worker.delayed_refresh_task")
@mock.patch("mergify_engine.worker.Worker.monitoring_task")
@mock.patch("mergify_engine.worker.Worker.dedicated_workers_spawner_task")
@mock.patch("mergify_engine.worker.Worker.shared_stream_worker_task")
@mock.patch("mergify_engine.worker.Worker.wait_shutdown_complete")
@mock.patch("mergify_engine.worker.Worker.loop_and_sleep_forever")
def test_worker_start_except_shared(
    loop_and_sleep_forever: mock.Mock,
    wait_shutdown_complete: mock.Mock,
    shared_stream_worker_task: mock.Mock,
    dedicated_workers_spawner_task: mock.Mock,
    monitoring_task: mock.Mock,
    delayed_refresh_task: mock.Mock,
    setup_signals: mock.Mock,
) -> None:
    async def just_run_once(name, idle_time, func):
        await func()

    loop_and_sleep_forever.side_effect = just_run_once

    worker.main(
        ["--enabled-services=dedicated-stream,stream-monitoring,delayed-refresh"]
    )
    while not wait_shutdown_complete.called:
        time.sleep(0.01)
    assert not shared_stream_worker_task.called
    assert dedicated_workers_spawner_task.called
    assert monitoring_task.called
    assert delayed_refresh_task.called


async def test_get_shared_worker_ids(
    monkeypatch: pytest.MonkeyPatch,
    redis_stream: utils.RedisStream,
    redis_cache: utils.RedisCache,
) -> None:
    monkeypatch.setenv("DYNO", "worker-shared.1")
    assert worker.get_process_index_from_env() == 0
    w1 = worker.Worker(shared_stream_processes=2, shared_stream_tasks_per_process=30)
    assert w1.get_shared_worker_ids() == list(range(0, 30))
    assert w1.global_shared_tasks_count == 60
    s1 = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-8", None, w1._owners_cache
    )
    assert s1.should_handle_owner(
        github_types.GitHubAccountIdType(123), set(), w1.global_shared_tasks_count
    )

    monkeypatch.setenv("DYNO", "worker-shared.2")
    assert worker.get_process_index_from_env() == 1
    w2 = worker.Worker(shared_stream_processes=2, shared_stream_tasks_per_process=30)
    assert w2.get_shared_worker_ids() == list(range(30, 60))
    assert w2.global_shared_tasks_count == 60
    s2 = worker.StreamProcessor(
        redis_stream, redis_cache, "shared-38", None, w2._owners_cache
    )
    assert not s2.should_handle_owner(
        github_types.GitHubAccountIdType(123), set(), w2.global_shared_tasks_count
    )
