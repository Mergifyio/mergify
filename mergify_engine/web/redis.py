# -*- encoding: utf-8 -*-
#
# Copyright © 2021—2022 Mergify SAS
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
from mergify_engine import redis_utils


_REDIS_LINKS: redis_utils.RedisLinks

LOG = daiquiri.getLogger(__name__)


async def startup() -> None:
    global _REDIS_LINKS
    _REDIS_LINKS = redis_utils.RedisLinks(
        name="web",
        cache_max_connections=config.REDIS_STREAM_WEB_MAX_CONNECTIONS,
        stream_max_connections=config.REDIS_CACHE_WEB_MAX_CONNECTIONS,
        queue_max_connections=config.REDIS_QUEUE_WEB_MAX_CONNECTIONS,
        eventlogs_max_connections=config.REDIS_EVENTLOGS_WEB_MAX_CONNECTIONS,
    )


async def shutdown() -> None:
    LOG.info("asgi: starting redis shutdown")
    await _REDIS_LINKS.shutdown_all()
    LOG.info("asgi: finished redis shutdown")


async def get_redis_links() -> redis_utils.RedisLinks:
    global _REDIS_LINKS
    return _REDIS_LINKS
