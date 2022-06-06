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

import dataclasses
import functools
import hashlib
import typing
import uuid

import daiquiri
import ddtrace
import redis.asyncio as redispy

from mergify_engine import config
from mergify_engine import service


LOG = daiquiri.getLogger(__name__)


RedisCache = typing.NewType("RedisCache", "redispy.Redis[str]")
RedisStream = typing.NewType("RedisStream", "redispy.Redis[bytes]")
RedisQueue = typing.NewType("RedisQueue", "redispy.Redis[bytes]")

ScriptIdT = typing.NewType("ScriptIdT", uuid.UUID)

SCRIPTS: typing.Dict[ScriptIdT, typing.Tuple[str, str]] = {}


# TODO(sileht): Redis script management can be moved back to Redis.register_script() mechanism
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


# FIXME(sileht): We store Cache and Stream script into the same global object
# it works but if a script is loaded into two redis, this won't works as expected
# as the app will think it's already loaded while it's not...
async def load_script(
    redis: typing.Union[RedisCache, RedisStream], script_id: ScriptIdT
) -> None:
    global SCRIPTS
    sha, script = SCRIPTS[script_id]
    # FIXME(sileht): weird, this method is typed on redis-py
    newsha = await redis.script_load(script)  # type: ignore[no-untyped-call]
    if newsha != sha:
        LOG.error(
            "wrong redis script sha cached",
            script_id=script_id,
            sha=sha,
            newsha=newsha,
        )
        SCRIPTS[script_id] = (newsha, script)


async def load_scripts(redis: typing.Union[RedisCache, RedisStream]) -> None:
    # TODO(sileht): cleanup unused script, this is tricky, because during
    # deployment we have running in parallel due to the rolling upgrade:
    # * an old version of the asgi server
    # * a new version of the asgi server
    # * a new version of the backend
    global SCRIPTS
    scripts = list(SCRIPTS.items())  # order matter for zip bellow
    shas = [sha for _, (sha, _) in scripts]
    ids = [_id for _id, _ in scripts]
    exists = await redis.script_exists(*shas)  # type: ignore[no-untyped-call]
    for script_id, exist in zip(ids, exists):
        if not exist:
            await load_script(redis, script_id)


async def run_script(
    redis: typing.Union[RedisCache, RedisStream],
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
    return await redis.evalsha(sha, len(keys), *args)  # type: ignore[no-untyped-call]


@dataclasses.dataclass
class RedisLinks:
    name: str
    cache_max_connections: typing.Optional[int] = None
    stream_max_connections: typing.Optional[int] = None
    queue_max_connections: typing.Optional[int] = None

    @functools.cached_property
    def queue(self) -> RedisQueue:
        client = self.redis_from_url(
            "queue",
            config.QUEUE_URL,
            decode_responses=False,
            max_connections=self.queue_max_connections,
        )
        return RedisQueue(client)

    @functools.cached_property
    def stream(self) -> RedisStream:
        client = self.redis_from_url(
            "stream",
            config.STREAM_URL,
            decode_responses=False,
            max_connections=self.stream_max_connections,
        )
        return RedisStream(client)

    @functools.cached_property
    def cache(self) -> RedisCache:
        client = self.redis_from_url(
            "cache",
            config.STORAGE_URL,
            decode_responses=True,
            max_connections=self.cache_max_connections,
        )
        return RedisCache(client)

    @typing.overload
    def redis_from_url(
        self,  # FIXME(sileht): mypy is lost if the method is static...
        name: str,
        url: str,
        decode_responses: typing.Literal[True],
        max_connections: typing.Optional[int] = None,
    ) -> "redispy.Redis[str]":
        ...

    @typing.overload
    def redis_from_url(
        self,  # FIXME(sileht): mypy is lost if the method is static...
        name: str,
        url: str,
        decode_responses: typing.Literal[False],
        max_connections: typing.Optional[int] = None,
    ) -> "redispy.Redis[bytes]":
        ...

    def redis_from_url(
        self,  # FIXME(sileht): mypy is lost if the method is static...
        name: str,
        url: str,
        decode_responses: bool,
        max_connections: typing.Optional[int] = None,
    ) -> typing.Union["redispy.Redis[bytes]", "redispy.Redis[str]"]:

        options: typing.Dict[str, typing.Any] = {}
        if config.REDIS_SSL_VERIFY_MODE_CERT_NONE and url.startswith("rediss://"):
            options["ssl_check_hostname"] = False
            options["ssl_cert_reqs"] = None

        client = redispy.Redis.from_url(
            url,
            max_connections=max_connections,
            decode_responses=decode_responses,
            client_name=f"{service.SERVICE_NAME}/{self.name}/{name}",
            **options,
        )
        ddtrace.Pin.override(client, service=f"engine-redis-{name}")
        return client

    async def shutdown_all(self) -> None:
        if "cache" in self.__dict__:
            await self.cache.close(close_connection_pool=True)
        if "stream" in self.__dict__:
            await self.stream.close(close_connection_pool=True)
        if "queue" in self.__dict__:
            await self.queue.close(close_connection_pool=True)
