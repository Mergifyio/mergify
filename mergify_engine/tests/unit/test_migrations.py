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

import itertools
import typing
from unittest import mock

import pytest

from mergify_engine import migrations
from mergify_engine import utils


@pytest.mark.asyncio
async def test_all_migration_on_empty_db(redis_cache: utils.RedisCache) -> None:
    assert await redis_cache.keys() == []
    assert not await redis_cache.exists("migration-stamps")
    await migrations.run(redis_cache)
    assert not await redis_cache.exists("migration-stamps")
    assert await redis_cache.keys() == ["migration-stamps"]
    assert await redis_cache.get("migration-stamps") == "2"


@pytest.mark.asyncio
async def test_merge_train_hash(redis_cache: utils.RedisCache) -> None:
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

    await migrations._run_scripts(redis_cache, "cache", stop_at_version=1)

    assert await redis_cache.get("migration-stamps") == "1"

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
@mock.patch("mergify_engine.redis_utils.run_script", side_effect=Exception("NOWAY!"))
async def test_legacy_stamps(
    _: mock.Mock,
    redis_cache: utils.RedisCache,
) -> None:
    assert not await redis_cache.exists("migration-stamps")
    await redis_cache.set("MERGE_TRAIN_MIGRATION_DONE", "done")
    await migrations._convert_legacy_stamp_version(redis_cache)
    assert await redis_cache.get("migration-stamps") == "1"
    assert not await redis_cache.exists("MERGE_TRAIN_MIGRATION_DONE")


@pytest.mark.asyncio
async def test_migration_summary_sha(redis_cache: utils.RedisCache) -> None:
    assert not await redis_cache.exists("migration-stamps")
    summary_sha_data = sorted(
        (
            (123456, 567890, 123),
            (123456, 121521, 789),
            (424242, 424242, 5),
            (789789, 987654, 42),
            (789789, 987655, 43),
            (789789, 987656, 44),
            (147258, 369258, 456),
        )
    )
    for owner_id, repo_id, pull_number in summary_sha_data:
        await redis_cache.set(
            f"summary-sha~{owner_id}~{repo_id}~{pull_number}",
            f"DATA~{owner_id}~{repo_id}~{pull_number}",
        )

    assert sorted(await redis_cache.keys("*")) == [
        "summary-sha~123456~121521~789",
        "summary-sha~123456~567890~123",
        "summary-sha~147258~369258~456",
        "summary-sha~424242~424242~5",
        "summary-sha~789789~987654~42",
        "summary-sha~789789~987655~43",
        "summary-sha~789789~987656~44",
    ]

    await migrations._stamp_version(redis_cache, 1)
    assert await redis_cache.get("migration-stamps") == "1"

    await migrations._run_scripts(redis_cache, "cache", stop_at_version=2)

    assert await redis_cache.get("migration-stamps") == "2"
    assert sorted(await redis_cache.keys("*")) == [
        "migration-stamps",
        "summary-sha~123456",
        "summary-sha~147258",
        "summary-sha~424242",
        "summary-sha~789789",
    ]

    for owner_id, data_iter in itertools.groupby(summary_sha_data, lambda x: x[0]):
        data = list(data_iter)
        key = f"summary-sha~{owner_id}"
        assert await redis_cache.hlen(key) == len(data)
        for _, repo_id, pull_number in data:
            subkey = f"{repo_id}~{pull_number}"
            expected_data = f"DATA~{owner_id}~{repo_id}~{pull_number}"
            assert await redis_cache.hget(key, subkey) == expected_data
