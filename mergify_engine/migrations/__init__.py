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

import daiquiri
import pkg_resources

from mergify_engine import redis_utils
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)

LEGACY_MERGE_TRAIN_MIRATION_STAMP_KEY = "MERGE_TRAIN_MIGRATION_DONE"

MIGRATION_STAMPS_KEY = "migration-stamps"


async def run(redis_cache: utils.RedisCache) -> None:
    # Convert old migration stamp key to new system
    if await redis_cache.exists(LEGACY_MERGE_TRAIN_MIRATION_STAMP_KEY):
        await redis_cache.set(MIGRATION_STAMPS_KEY, 1)
        await redis_cache.delete(LEGACY_MERGE_TRAIN_MIRATION_STAMP_KEY)

    await _run_scripts("cache", redis_cache)


async def _run_scripts(
    dirname: str, redis: typing.Union[utils.RedisCache, utils.RedisStream]
) -> None:
    current_version = await redis.get(MIGRATION_STAMPS_KEY)
    if current_version is None:
        current_version = 0
    else:
        current_version = int(current_version)

    files = pkg_resources.resource_listdir(__name__, dirname)
    for script in sorted(files):
        version_str = script.partition("_")[0]
        if len(version_str) != 4:
            raise RuntimeError(f"Invalid redis migration script name: {script}")
        try:
            version = int(version_str)
        except ValueError:
            raise RuntimeError(f"Invalid redis migration script name: {script}")

        if version <= current_version:
            continue

        raw = pkg_resources.resource_string(__name__, f"{dirname}/{script}").decode()
        redis_script = redis_utils.register_script(raw)
        await redis_utils.run_script(redis, redis_script, ())
        current_version = version
        await redis.set(MIGRATION_STAMPS_KEY, current_version)
