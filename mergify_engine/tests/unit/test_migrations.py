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

import typing
from unittest import mock

import pytest

from mergify_engine import migrations
from mergify_engine import utils


@pytest.mark.asyncio
async def test_0001_merge_train_hash(
    redis_cache: utils.RedisCache,
) -> None:
    assert not await redis_cache.exists("migration-stamps")
    expected_trains: typing.Dict[int, typing.Dict[str, str]] = {}
    for owner_id in (123456, 424242, 789789):
        expected_trains.setdefault(owner_id, {})
        for i in range(1, 3):
            for ref in ("main", "stable"):
                repo_id = owner_id + i
                expected_trains[owner_id][f"{repo_id}~{ref}"] = "some-data"
                await redis_cache.set(
                    f"merge-train~{owner_id}~{repo_id}~{ref}", "some-data"
                )

    await migrations.run(redis_cache)

    assert await redis_cache.get("migration-stamps") == "3"

    old_trains = sorted(await redis_cache.keys("merge-train~*"))
    assert old_trains == []
    trains = sorted(await redis_cache.keys("merge-trains~*"))
    assert trains == [
        "merge-trains~123456",
        "merge-trains~424242",
        "merge-trains~789789",
    ]

    for train in trains:
        owner_id = int(train[13:])
        assert await redis_cache.hgetall(train) == expected_trains[owner_id]


@pytest.mark.asyncio
@mock.patch("mergify_engine.migrations._run_scripts")
async def test_legacy_stamps(
    _: mock.Mock,
    redis_cache: utils.RedisCache,
) -> None:

    await redis_cache.set("MERGE_TRAIN_MIGRATION_DONE", "done")
    await migrations.run(redis_cache)
    assert await redis_cache.get("migration-stamps") == "1"


@pytest.mark.asyncio
async def test_0002_pull_request_sha_number(
    redis_cache: utils.RedisCache,
) -> None:
    assert not await redis_cache.exists("migration-stamps")

    for i in range(20):
        await redis_cache.set(f"sha~{i}~{i}~{i}", f"{i}")

    for i in range(20):
        await redis_cache.set(f"summary-sha~{i}~{i}~{i}", f"{i}")

    assert len(await redis_cache.keys("*")) == 40

    await migrations.run(redis_cache)

    assert len(await redis_cache.keys("*")) == 21

    assert await redis_cache.get("migration-stamps") == "3"

    for key in await redis_cache.keys("*"):
        assert key == "migration-stamps" or key.startswith("summary-sha~")


@pytest.mark.asyncio
async def test_0003_attempts(
    redis_cache: utils.RedisCache,
) -> None:
    assert not await redis_cache.exists("migration-stamps")

    for i in range(20):
        await redis_cache.hset("attempts", f"pull~132~foobar~{i}", 1)

    for i in range(5):
        await redis_cache.hset("attempts", f"bucket~{i}~owner", 1)

    for i in range(5):
        await redis_cache.hset("attempts", f"bucket-sources~{i}~repo~{i}", 1)

    assert len(await redis_cache.keys("*")) == 1
    assert len(await redis_cache.hkeys("attempts")) == 30

    await migrations.run(redis_cache)

    assert len(await redis_cache.keys("*")) == 2
    assert len(await redis_cache.hkeys("attempts")) == 10

    assert await redis_cache.get("migration-stamps") == "3"

    for key in await redis_cache.hkeys("attempts"):
        assert key.startswith("bucket")
