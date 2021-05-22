# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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

import daiquiri

from mergify_engine import config
from mergify_engine import utils


_AREDIS_STREAM: utils.RedisStream
_AREDIS_CACHE: utils.RedisCache

LOG = daiquiri.getLogger(__name__)


async def startup() -> None:
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_STREAM = utils.create_aredis_for_stream(
        max_connections=config.REDIS_STREAM_WEB_MAX_CONNECTIONS
    )
    _AREDIS_CACHE = utils.create_aredis_for_cache(
        max_connections=config.REDIS_CACHE_WEB_MAX_CONNECTIONS
    )


async def shutdown() -> None:
    LOG.info("asgi: starting redis shutdown")
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_CACHE.connection_pool.max_idle_time = 0
    _AREDIS_CACHE.connection_pool.disconnect()
    _AREDIS_STREAM.connection_pool.max_idle_time = 0
    _AREDIS_STREAM.connection_pool.disconnect()
    LOG.info("asgi: waiting redis pending tasks to complete")
    await utils.stop_pending_aredis_tasks()
    LOG.info("asgi: finished redis shutdown")


async def get_redis_stream():
    global _AREDIS_STREAM
    return _AREDIS_STREAM


async def get_redis_cache():
    global _AREDIS_CACHE
    return _AREDIS_CACHE
