#
# Copyright © 2019–2021 Mergify SAS
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
import functools
import hashlib
import ssl
import typing
import uuid

import daiquiri
import yaaredis

from mergify_engine import config


LOG = daiquiri.getLogger(__name__)


# NOTE(sileht): I wonder why mypy thinks `yaaredis.StrictRedis` is `typing.Any`...
RedisCache = typing.NewType("RedisCache", yaaredis.StrictRedis)  # type: ignore
RedisStream = typing.NewType("RedisStream", yaaredis.StrictRedis)  # type: ignore
RedisQueue = typing.NewType("RedisQueue", yaaredis.StrictRedis)  # type: ignore

ScriptIdT = typing.NewType("ScriptIdT", uuid.UUID)

SCRIPTS: typing.Dict[ScriptIdT, typing.Tuple[str, str]] = {}


def register_script(script: str) -> ScriptIdT:
    global SCRIPTS
    # NOTE(sileht): We don't use sha, in case of something server side change the script sha
    script_id = ScriptIdT(uuid.uuid4())
    SCRIPTS[script_id] = (
        # NOTE(sileht): SHA1 is imposed by Redis itself
        hashlib.sha1(  # nosemgrep contrib.dlint.dlint-equivalent.insecure-hashlib-use, python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
            script.encode("utf8")
        ).hexdigest(),
        script,
    )
    return script_id


async def load_script(redis: RedisStream, script_id: ScriptIdT) -> None:
    global SCRIPTS
    sha, script = SCRIPTS[script_id]
    newsha = await redis.script_load(script)
    if newsha != sha:
        LOG.error(
            "wrong redis script sha cached",
            script_id=script_id,
            sha=sha,
            newsha=newsha,
        )
        SCRIPTS[script_id] = (newsha, script)


async def load_scripts(redis: RedisStream) -> None:
    # TODO(sileht): cleanup unused script, this is tricky, because during
    # deployment we have running in parallel due to the rolling upgrade:
    # * an old version of the asgi server
    # * a new version of the asgi server
    # * a new version of the backend
    global SCRIPTS
    ids = list(SCRIPTS.keys())
    exists = await redis.script_exists(*ids)
    for script_id, exist in zip(ids, exists):
        if not exist:
            await load_script(redis, script_id)


async def run_script(
    redis: RedisStream,
    script_id: ScriptIdT,
    keys: typing.Tuple[str, ...],
    args: typing.Optional[typing.Tuple[typing.Union[str], ...]] = None,
) -> typing.Any:
    global SCRIPTS
    sha, script = SCRIPTS[script_id]
    if args is None:
        args = keys
    else:
        args = keys + args
    return await redis.evalsha(sha, len(keys), *args)


async def stop_pending_yaaredis_tasks() -> None:
    tasks = [
        task
        for task in asyncio.all_tasks()
        if (
            getattr(task.get_coro(), "__qualname__", None)
            == "ConnectionPool.disconnect_on_idle_time_exceeded"
        )
    ]

    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)


@dataclasses.dataclass
class RedisLinks:
    max_idle_time: int = 60
    cache_max_connections: typing.Optional[int] = None
    stream_max_connections: typing.Optional[int] = None
    queue_max_connections: typing.Optional[int] = None

    @functools.cached_property
    def queue(self) -> RedisCache:
        client = self.redis_from_url(
            config.QUEUE_URL,
            max_idle_time=self.max_idle_time,
            max_connections=self.queue_max_connections,
        )
        return RedisQueue(client)

    @functools.cached_property
    def stream(self) -> RedisCache:
        client = self.redis_from_url(
            config.STREAM_URL,
            max_idle_time=self.max_idle_time,
            max_connections=self.stream_max_connections,
        )
        return RedisCache(client)

    @functools.cached_property
    def cache(self) -> RedisCache:
        client = self.redis_from_url(
            config.STORAGE_URL,
            decode_responses=True,
            max_idle_time=self.max_idle_time,
            max_connections=self.cache_max_connections,
        )
        return RedisCache(client)

    @staticmethod
    def redis_from_url(url: str, **options: typing.Any) -> yaaredis.StrictRedis:
        ssl_scheme = "rediss://"
        if config.REDIS_SSL_VERIFY_MODE_CERT_NONE and url.startswith(ssl_scheme):
            final_url = f"redis://{url[len(ssl_scheme):]}"
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = (
                ssl.CERT_NONE  # nosemgrep contrib.dlint.dlint-equivalent.insecure-ssl-use
            )
            options["ssl_context"] = ctx
        else:
            final_url = url
        return yaaredis.StrictRedis.from_url(final_url, **options)

    def shutdown_all(self) -> None:
        self.cache.connection_pool.max_idle_time = 0
        self.cache.connection_pool.disconnect()
        self.stream.connection_pool.max_idle_time = 0
        self.stream.connection_pool.disconnect()
        self.queue.connection_pool.max_idle_time = 0
        self.queue.connection_pool.disconnect()
