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

import argparse
import asyncio
import collections
import contextlib
import dataclasses
import datetime
import functools
import hashlib
import itertools
import os
import signal
import time
import typing

import aredis
import daiquiri
from datadog import statsd
import msgpack
import tenacity

from mergify_engine import config
from mergify_engine import context
from mergify_engine import date
from mergify_engine import delayed_refresh
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import logs
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.queue import merge_train


try:
    import vcr
except ImportError:

    class vcr_errors_CannotOverwriteExistingCassetteException(Exception):
        pass


else:
    vcr_errors_CannotOverwriteExistingCassetteException: Exception = (  # type: ignore
        vcr.errors.CannotOverwriteExistingCassetteException
    )


LOG = daiquiri.getLogger(__name__)


MAX_RETRIES: int = 3
WORKER_PROCESSING_DELAY: float = 30
STREAM_ATTEMPTS_LOGGING_THRESHOLD: int = 20


StreamNameType = typing.NewType("StreamNameType", str)


class IgnoredException(Exception):
    pass


@dataclasses.dataclass
class PullRetry(Exception):
    attempts: int


class MaxPullRetry(PullRetry):
    pass


@dataclasses.dataclass
class StreamRetry(Exception):
    stream_name: StreamNameType
    attempts: int
    retry_at: datetime.datetime


class StreamUnused(Exception):
    stream_name: StreamNameType


T_MessagePayload = typing.NewType("T_MessagePayload", typing.Dict[bytes, bytes])
T_MessageID = typing.NewType("T_MessageID", str)


class T_PayloadEvent(typing.TypedDict):
    owner: github_types.GitHubLogin
    repo: github_types.GitHubRepositoryName
    pull_number: github_types.GitHubPullRequestNumber
    source: context.T_PayloadEventSource


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),  # type: ignore[no-untyped-call]
    stop=tenacity.stop_after_attempt(5),  # type: ignore[no-untyped-call]
    retry=tenacity.retry_if_exception_type(aredis.ConnectionError),  # type: ignore[no-untyped-call]
    reraise=True,
)
async def push(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner: github_types.GitHubLogin,
    repo_id: github_types.GitHubRepositoryIdType,
    repo: github_types.GitHubRepositoryName,
    pull_number: typing.Optional[github_types.GitHubPullRequestNumber],
    event_type: github_types.GitHubEventType,
    data: github_types.GitHubEvent,
) -> typing.Tuple[T_MessageID, T_MessagePayload]:
    stream_name = f"stream~{owner}~{owner_id}"
    scheduled_at = date.utcnow() + datetime.timedelta(seconds=WORKER_PROCESSING_DELAY)
    score = scheduled_at.timestamp()
    transaction = await redis.pipeline()
    # NOTE(sileht): Add this event to the pull request stream
    payload = T_MessagePayload(
        {
            b"event": msgpack.packb(
                {
                    "owner_id": owner_id,
                    "repo_id": repo_id,
                    "owner": owner,
                    "repo": repo,
                    "pull_number": pull_number,
                    "source": {
                        "event_type": event_type,
                        "data": data,
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                    },
                },
                use_bin_type=True,
            ),
        }
    )

    await transaction.xadd(stream_name, payload)
    # NOTE(sileht): Add pull request stream to process to the list, only if it
    # does not exists, to not update the score(date)
    await transaction.zaddoption("streams", "NX", **{stream_name: score})
    message_id: T_MessageID = (await transaction.execute())[0]
    LOG.debug(
        "pushed to worker",
        gh_owner=owner,
        gh_repo=repo,
        gh_pull=pull_number,
        event_type=event_type,
    )
    return (message_id, payload)


async def run_engine(
    installation: context.Installation,
    repo_id: github_types.GitHubRepositoryIdType,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: github_types.GitHubPullRequestNumber,
    sources: typing.List[context.T_PayloadEventSource],
) -> None:
    logger = daiquiri.getLogger(
        __name__,
        gh_repo=repo_name,
        gh_owner=installation.owner_login,
        gh_pull=pull_number,
    )
    logger.debug("engine in thread start")
    try:
        started_at = date.utcnow()
        try:
            ctxt = await installation.get_pull_request_context(repo_id, pull_number)
        except http.HTTPNotFound:
            # NOTE(sileht): Don't fail if we received even on repo/pull that doesn't exists anymore
            logger.debug("pull request doesn't exists, skipping it")
            return None

        result = await engine.run(ctxt, sources)
        if result is not None:
            result.started_at = started_at
            result.ended_at = date.utcnow()
            await ctxt.set_summary_check(result)
    finally:
        logger.debug("engine in thread end")


PullsToConsume = typing.NewType(
    "PullsToConsume",
    collections.OrderedDict[
        typing.Tuple[
            github_types.GitHubRepositoryName,
            github_types.GitHubRepositoryIdType,
            github_types.GitHubPullRequestNumber,
        ],
        typing.Tuple[
            typing.List[T_MessageID], typing.List[context.T_PayloadEventSource]
        ],
    ],
)


@dataclasses.dataclass
class StreamSelector:
    redis_stream: utils.RedisStream
    worker_id: int
    worker_count: int

    def get_worker_id_for(self, stream: bytes) -> int:
        return int(hashlib.md5(stream).hexdigest(), 16) % self.worker_count  # nosec

    def _is_stream_for_me(self, stream: bytes) -> bool:
        return self.get_worker_id_for(stream) == self.worker_id

    async def next_stream(self) -> typing.Optional[StreamNameType]:
        now = time.time()
        for stream in await self.redis_stream.zrangebyscore(
            "streams",
            min=0,
            max=now,
        ):
            if self._is_stream_for_me(stream):
                statsd.increment(
                    "engine.streams.selected", tags=[f"worker_id:{self.worker_id}"]
                )
                return StreamNameType(stream.decode())

        return None


@dataclasses.dataclass
class StreamProcessor:
    redis_stream: utils.RedisStream
    redis_cache: utils.RedisCache

    @contextlib.asynccontextmanager
    async def _translate_exception_to_retries(
        self,
        stream_name: StreamNameType,
        attempts_key: typing.Optional[str] = None,
    ) -> typing.AsyncIterator[None]:
        try:
            yield
        except Exception as e:
            if isinstance(e, aredis.exceptions.ConnectionError):
                statsd.increment("redis.client.connection.errors")

            if isinstance(e, exceptions.MergeableStateUnknown) and attempts_key:
                attempts = await self.redis_stream.hincrby("attempts", attempts_key)
                if attempts < MAX_RETRIES:
                    raise PullRetry(attempts) from e
                else:
                    await self.redis_stream.hdel("attempts", attempts_key)
                    raise MaxPullRetry(attempts) from e

            if isinstance(e, exceptions.MergifyNotInstalled):
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", stream_name)
                raise StreamUnused(stream_name)

            if isinstance(e, github.TooManyPages):
                # TODO(sileht): Ideally this should be catcher earlier to post an
                # appropriate check-runs to inform user the PR is too big to be handled
                # by Mergify, but this need a bit of refactory to do it, so in the
                # meantimes...
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", stream_name)
                raise IgnoredException()

            if exceptions.should_be_ignored(e):
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", stream_name)
                raise IgnoredException()

            if isinstance(e, exceptions.RateLimited):
                retry_at = date.utcnow() + e.countdown
                score = retry_at.timestamp()
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", stream_name)
                await self.redis_stream.zaddoption(
                    "streams", "XX", **{stream_name: score}
                )
                raise StreamRetry(stream_name, 0, retry_at)

            backoff = exceptions.need_retry(e)
            if backoff is None:
                # NOTE(sileht): This is our fault, so retry until we fix the bug but
                # without increasing the attempts
                raise

            attempts = await self.redis_stream.hincrby("attempts", stream_name)
            retry_in = 3 ** min(attempts, 3) * backoff
            retry_at = date.utcnow() + retry_in
            score = retry_at.timestamp()
            await self.redis_stream.zaddoption("streams", "XX", **{stream_name: score})
            raise StreamRetry(stream_name, attempts, retry_at)

    def _extract_owner(
        self, stream_name: StreamNameType
    ) -> typing.Tuple[github_types.GitHubLogin, github_types.GitHubAccountIdType]:
        stream_splitted = stream_name.split("~")[1:]
        return (
            github_types.GitHubLogin(stream_splitted[0]),
            github_types.GitHubAccountIdType(int(stream_splitted[1])),
        )

    async def consume(self, stream_name: StreamNameType) -> None:
        owner_login, owner_id = self._extract_owner(stream_name)
        LOG.debug("consoming stream", gh_owner=owner_login)

        try:
            async with self._translate_exception_to_retries(stream_name):
                sub = await subscription.Subscription.get_subscription(
                    self.redis_cache, owner_id
                )
            async with github.aget_client(owner_login) as client:
                installation = context.Installation(
                    owner_id, owner_login, sub, client, self.redis_cache
                )
                async with self._translate_exception_to_retries(stream_name):
                    pulls = await self._extract_pulls_from_stream(
                        stream_name, installation
                    )
                if pulls:
                    client.set_requests_ratio(len(pulls))
                    await self._consume_pulls(stream_name, installation, pulls)

                await self._refresh_merge_trains(stream_name, installation)
        except aredis.exceptions.ConnectionError:
            statsd.increment("redis.client.connection.errors")
            LOG.warning(
                "Stream Processor lost Redis connection", stream_name=stream_name
            )
        except StreamUnused:
            LOG.info("unused stream, dropping it", gh_owner=owner_login, exc_info=True)
            try:
                await self.redis_stream.delete(stream_name)
            except aredis.exceptions.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning(
                    "fail to drop stream, it will be retried", stream_name=stream_name
                )
        except StreamRetry as e:
            log_method = (
                LOG.error
                if e.attempts >= STREAM_ATTEMPTS_LOGGING_THRESHOLD
                else LOG.info
            )
            log_method(
                "failed to process stream, retrying",
                attempts=e.attempts,
                retry_at=e.retry_at,
                gh_owner=owner_login,
                exc_info=True,
            )
            return
        except vcr_errors_CannotOverwriteExistingCassetteException:
            messages = await self.redis_stream.xrange(
                stream_name, count=config.STREAM_MAX_BATCH
            )
            for message_id, message in messages:
                LOG.info(msgpack.unpackb(message[b"event"], raw=False))
                await self.redis_stream.execute_command("XDEL", stream_name, message_id)

        except Exception:
            # Ignore it, it will retried later
            LOG.error("failed to process stream", gh_owner=owner_login, exc_info=True)

        LOG.debug("cleanup stream start", stream_name=stream_name)
        try:
            await self.redis_stream.eval(
                self.ATOMIC_CLEAN_STREAM_SCRIPT, 1, stream_name.encode(), time.time()
            )
        except aredis.exceptions.ConnectionError:
            statsd.increment("redis.client.connection.errors")
            LOG.warning(
                "fail to cleanup stream, it maybe partially replayed",
                stream_name=stream_name,
            )
        LOG.debug("cleanup stream end", stream_name=stream_name)

    async def _refresh_merge_trains(
        self, stream_name: StreamNameType, installation: context.Installation
    ) -> None:
        async with self._translate_exception_to_retries(
            stream_name,
        ):
            async for train in merge_train.Train.iter_trains(installation):
                await train.load()
                await train.refresh()

    # NOTE(sileht): If the stream still have messages, we update the score to reschedule the
    # pull later
    ATOMIC_CLEAN_STREAM_SCRIPT = """
local stream_name = KEYS[1]
local score = ARGV[1]

redis.call("HDEL", "attempts", stream_name)

local len = tonumber(redis.call("XLEN", stream_name))
if len == 0 then
    redis.call("ZREM", "streams", stream_name)
    redis.call("DEL", stream_name)
else
    redis.call("ZADD", "streams", score, stream_name)
end
"""

    async def _extract_pulls_from_stream(
        self, stream_name: StreamNameType, installation: context.Installation
    ) -> PullsToConsume:
        messages: typing.List[
            typing.Tuple[T_MessageID, T_MessagePayload]
        ] = await self.redis_stream.xrange(stream_name, count=config.STREAM_MAX_BATCH)
        LOG.debug(
            "read stream",
            stream_name=stream_name,
            messages_count=len(messages),
        )
        statsd.histogram("engine.streams.size", len(messages))  # type: ignore[no-untyped-call]
        statsd.gauge("engine.streams.max_size", config.STREAM_MAX_BATCH)

        # TODO(sileht): Put this cache in Repository context
        opened_pulls_by_repo: typing.Dict[
            github_types.GitHubRepositoryName,
            typing.List[github_types.GitHubPullRequest],
        ] = {}

        # Groups stream by pull request
        pulls: PullsToConsume = PullsToConsume(collections.OrderedDict())
        for message_id, message in messages:
            data = msgpack.unpackb(message[b"event"], raw=False)
            repo_name = github_types.GitHubRepositoryName(data["repo"])
            repo_id = github_types.GitHubRepositoryIdType(data["repo_id"])
            source = typing.cast(context.T_PayloadEventSource, data["source"])
            if data["pull_number"] is not None:
                key = (
                    repo_name,
                    repo_id,
                    github_types.GitHubPullRequestNumber(data["pull_number"]),
                )
                group = pulls.setdefault(key, ([], []))
                group[0].append(message_id)
                group[1].append(source)
            else:
                logger = daiquiri.getLogger(
                    __name__,
                    gh_repo=repo_name,
                    gh_owner=installation.owner_login,
                    source=source,
                )
                if repo_name not in opened_pulls_by_repo:
                    try:
                        opened_pulls_by_repo[repo_name] = [
                            p
                            async for p in installation.client.items(
                                f"/repos/{installation.owner_login}/{repo_name}/pulls"
                            )
                        ]
                    except Exception as e:
                        if exceptions.should_be_ignored(e):
                            opened_pulls_by_repo[repo_name] = []
                        else:
                            raise

                converted_messages = await self._convert_event_to_messages(
                    installation,
                    repo_id,
                    repo_name,
                    source,
                    opened_pulls_by_repo[repo_name],
                )

                logger.debug("event unpacked into %s messages", len(converted_messages))
                messages.extend(converted_messages)
                deleted = await self.redis_stream.xdel(stream_name, message_id)
                if deleted != 1:
                    # FIXME(sileht): During shutdown, heroku may have already started
                    # another worker that have already take the lead of this stream_name
                    # This can create duplicate events in the streams but that should not
                    # be a big deal as the engine will not been ran by the worker that's
                    # shutdowning.
                    contents = await self.redis_stream.xrange(
                        stream_name, start=message_id, end=message_id
                    )
                    if contents:
                        logger.error(
                            "message `%s` have not been deleted has expected, "
                            "(result: %s), content of current message id: %s",
                            message_id,
                            deleted,
                            contents,
                        )
        return pulls

    async def _convert_event_to_messages(
        self,
        installation: context.Installation,
        repo_id: github_types.GitHubRepositoryIdType,
        repo_name: github_types.GitHubRepositoryName,
        source: context.T_PayloadEventSource,
        pulls: typing.List[github_types.GitHubPullRequest],
    ) -> typing.List[typing.Tuple[T_MessageID, T_MessagePayload]]:
        # NOTE(sileht): the event is incomplete (push, refresh, checks, status)
        # So we get missing pull numbers, add them to the stream to
        # handle retry later, add them to message to run engine on them now,
        # and delete the current message_id as we have unpack this incomplete event into
        # multiple complete event
        pull_numbers = await github_events.extract_pull_numbers_from_event(
            installation,
            repo_name,
            source["event_type"],
            source["data"],
            pulls,
        )

        messages = []
        for pull_number in pull_numbers:
            if pull_number is None:
                # NOTE(sileht): even it looks not possible, this is a safeguard to ensure
                # we didn't generate a ending loop of events, because when pull_number is
                # None, this method got called again and again.
                raise RuntimeError("Got an empty pull number")
            messages.append(
                await push(
                    self.redis_stream,
                    installation.owner_id,
                    installation.owner_login,
                    repo_id,
                    repo_name,
                    pull_number,
                    source["event_type"],
                    source["data"],
                )
            )
        return messages

    async def _consume_pulls(
        self,
        stream_name: StreamNameType,
        installation: context.Installation,
        pulls: PullsToConsume,
    ) -> None:
        LOG.debug("stream contains %d pulls", len(pulls), stream_name=stream_name)
        for (repo, repo_id, pull_number), (message_ids, sources) in pulls.items():

            statsd.histogram("engine.streams.batch-size", len(sources))  # type: ignore[no-untyped-call]
            for source in sources:
                if "timestamp" in source:
                    statsd.histogram(  # type: ignore[no-untyped-call]
                        "engine.streams.events.latency",
                        (
                            datetime.datetime.utcnow()
                            - datetime.datetime.fromisoformat(source["timestamp"])
                        ).total_seconds(),
                    )

            logger = daiquiri.getLogger(
                __name__,
                gh_repo=repo,
                gh_owner=installation.owner_login,
                gh_pull=pull_number,
            )

            attempts_key = f"pull~{installation.owner_login}~{repo}~{pull_number}"
            try:
                async with self._translate_exception_to_retries(
                    stream_name, attempts_key
                ):
                    await run_engine(installation, repo_id, repo, pull_number, sources)
                await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.execute_command(
                    "XDEL", stream_name, *message_ids
                )
            except IgnoredException:
                await self.redis_stream.execute_command(
                    "XDEL", stream_name, *message_ids
                )
                logger.debug("failed to process pull request, ignoring", exc_info=True)
            except MaxPullRetry as e:
                await self.redis_stream.execute_command(
                    "XDEL", stream_name, *message_ids
                )
                logger.error(
                    "failed to process pull request, abandoning",
                    attempts=e.attempts,
                    exc_info=True,
                )
            except PullRetry as e:
                logger.info(
                    "failed to process pull request, retrying",
                    attempts=e.attempts,
                    exc_info=True,
                )
            except StreamRetry:
                raise
            except StreamUnused:
                raise
            except vcr_errors_CannotOverwriteExistingCassetteException:
                raise
            except Exception:
                # Ignore it, it will retried later
                logger.error("failed to process pull request", exc_info=True)


def get_process_index_from_env() -> int:
    dyno = os.getenv("DYNO", None)
    if dyno:
        return int(dyno.rsplit(".", 1)[-1]) - 1
    else:
        return 0


@dataclasses.dataclass
class Worker:
    idle_sleep_time: float = 0.42
    shutdown_timeout: float = config.WORKER_SHUTDOWN_TIMEOUT
    worker_per_process: int = config.STREAM_WORKERS_PER_PROCESS
    process_count: int = config.STREAM_PROCESSES
    process_index: int = dataclasses.field(default_factory=get_process_index_from_env)
    enabled_services: typing.Set[
        typing.Literal["stream", "stream-monitoring", "delayed-refresh"]
    ] = dataclasses.field(
        default_factory=lambda: {"stream", "stream-monitoring", "delayed-refresh"}
    )

    _redis_stream: typing.Optional[utils.RedisStream] = dataclasses.field(
        init=False, default=None
    )
    _redis_cache: typing.Optional[utils.RedisCache] = dataclasses.field(
        init=False, default=None
    )

    _loop: asyncio.AbstractEventLoop = dataclasses.field(
        init=False, default_factory=asyncio.get_running_loop
    )
    _stopping: asyncio.Event = dataclasses.field(
        init=False, default_factory=asyncio.Event
    )

    _worker_tasks: typing.List[asyncio.Task[None]] = dataclasses.field(
        init=False, default_factory=list
    )
    _stream_monitoring_task: typing.Optional[asyncio.Task[None]] = dataclasses.field(
        init=False, default=None
    )

    @property
    def worker_count(self):
        return self.worker_per_process * self.process_count

    async def stream_worker_task(self, worker_id: int) -> None:
        if self._redis_stream is None or self._redis_cache is None:
            raise RuntimeError("redis clients are not ready")

        log_context_token = logs.WORKER_ID.set(worker_id)

        # NOTE(sileht): This task must never fail, we don't want to write code to
        # reap/clean/respawn them
        stream_processor = StreamProcessor(self._redis_stream, self._redis_cache)
        stream_selector = StreamSelector(
            self._redis_stream, worker_id, self.worker_count
        )

        while not self._stopping.is_set():
            try:
                stream_name = await stream_selector.next_stream()
                if stream_name:
                    LOG.debug("worker %s take stream: %s", worker_id, stream_name)
                    try:
                        with statsd.timed("engine.stream.consume.time"):  # type: ignore[no-untyped-call]
                            await stream_processor.consume(stream_name)
                    finally:
                        LOG.debug(
                            "worker %s release stream: %s",
                            worker_id,
                            stream_name,
                        )
                else:
                    LOG.debug("worker %s has nothing to do, sleeping a bit", worker_id)
                    await self._sleep_or_stop()
            except asyncio.CancelledError:
                LOG.debug("worker %s killed", worker_id)
                return
            except aredis.exceptions.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning("worker lost Redis connection", worker_id, exc_info=True)
                await self._sleep_or_stop()
            except Exception:
                LOG.error("worker %s fail, sleeping a bit", worker_id, exc_info=True)
                await self._sleep_or_stop()

        LOG.debug("worker %s exited", worker_id)
        logs.WORKER_ID.reset(log_context_token)

    async def _sleep_or_stop(self, timeout: typing.Optional[float] = None) -> None:
        if timeout is None:
            timeout = self.idle_sleep_time
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def monitoring_task(self) -> None:
        if self._redis_stream is None or self._redis_cache is None:
            raise RuntimeError("redis clients are not ready")

        while not self._stopping.is_set():
            try:
                now = time.time()
                streams = await self._redis_stream.zrangebyscore(
                    "streams",
                    min=0,
                    max=now,
                    withscores=True,
                )
                # NOTE(sileht): The latency may not be exact with the next StreamSelector
                # based on hash+modulo
                if len(streams) > self.worker_count:
                    latency = now - streams[self.worker_count][1]
                    statsd.timing("engine.streams.latency", latency)  # type: ignore[no-untyped-call]
                else:
                    statsd.timing("engine.streams.latency", 0)  # type: ignore[no-untyped-call]

                statsd.gauge("engine.streams.backlog", len(streams))
                statsd.gauge("engine.workers.count", self.worker_count)
                statsd.gauge("engine.processes.count", self.process_count)
                statsd.gauge(
                    "engine.workers-per-process.count", self.worker_per_process
                )
            except asyncio.CancelledError:
                LOG.debug("monitoring task killed")
                return
            except aredis.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning("monitoring task lost Redis connection", exc_info=True)
            except Exception:
                LOG.error("monitoring task failed", exc_info=True)

            await self._sleep_or_stop(60)

    async def delayed_refresh_task(self) -> None:
        if self._redis_stream is None or self._redis_cache is None:
            raise RuntimeError("redis clients are not ready")

        while not self._stopping.is_set():
            try:
                await delayed_refresh.send(self._redis_stream, self._redis_cache)
            except asyncio.CancelledError:
                LOG.debug("delayed refresh task killed")
                return
            except aredis.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning("delayed refresh task lost Redis connection", exc_info=True)
            except Exception:
                LOG.error("delayed refresh task failed", exc_info=True)

            await self._sleep_or_stop(60)

    def get_worker_ids(self) -> typing.List[int]:
        return list(
            range(
                self.process_index * self.worker_per_process,
                (self.process_index + 1) * self.worker_per_process,
            )
        )

    async def start(self) -> None:
        self._stopping.clear()

        self._redis_stream = utils.create_aredis_for_stream()
        self._redis_cache = utils.create_aredis_for_cache()

        if "stream" in self.enabled_services:
            worker_ids = self.get_worker_ids()
            LOG.info("workers starting", count=len(worker_ids))
            for worker_id in worker_ids:
                self._worker_tasks.append(
                    asyncio.create_task(self.stream_worker_task(worker_id))
                )
            LOG.info("workers started", count=len(worker_ids))

        if "delayed-refresh" in self.enabled_services:
            LOG.info("delayed refresh starting")
            self._delayed_refresh_task = asyncio.create_task(
                self.delayed_refresh_task()
            )
            LOG.info("delayed refresh started")

        if "stream-monitoring" in self.enabled_services:
            LOG.info("monitoring starting")
            self._stream_monitoring_task = asyncio.create_task(self.monitoring_task())
            LOG.info("monitoring started")

    async def _shutdown(self) -> None:
        tasks = []
        tasks.extend(self._worker_tasks)
        if self._delayed_refresh_task is not None:
            tasks.append(self._delayed_refresh_task)
        if self._stream_monitoring_task is not None:
            tasks.append(self._stream_monitoring_task)

        LOG.info("workers and monitoring exiting", count=len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
        if pending:
            LOG.info("workers and monitoring being killed", count=len(pending))
            for task in pending:
                task.cancel(msg="shutdown")
            await asyncio.wait(pending)
        LOG.info("workers and monitoring exited", count=len(tasks))

        LOG.info("redis finalizing")
        self._worker_tasks = []
        if self._redis_stream:
            self._redis_stream.connection_pool.max_idle_time = 0
            self._redis_stream.connection_pool.disconnect()
            self._redis_stream = None

        if self._redis_cache:
            self._redis_cache.connection_pool.max_idle_time = 0
            self._redis_cache.connection_pool.disconnect()
            self._redis_cache = None

        await utils.stop_pending_aredis_tasks()
        LOG.info("redis finalized")

        LOG.info("shutdown finished")

    def stop(self) -> None:
        self._stopping.set()
        self._stop_task = asyncio.create_task(self._shutdown())

    async def wait_shutdown_complete(self) -> None:
        await self._stopping.wait()
        await self._stop_task

    def stop_with_signal(self, signame: str) -> None:
        if not self._stopping.is_set():
            LOG.info("got signal %s: cleanly shutdown workers", signame)
            self.stop()
        else:
            LOG.info("got signal %s: ignoring, shutdown already in process", signame)

    def setup_signals(self) -> None:
        for signame in ("SIGINT", "SIGTERM"):
            self._loop.add_signal_handler(
                getattr(signal, signame),
                functools.partial(self.stop_with_signal, signame),
            )


async def run_forever() -> None:
    worker = Worker()
    await worker.start()
    worker.setup_signals()
    await worker.wait_shutdown_complete()
    LOG.info("Exiting...")


def main() -> None:
    logs.setup_logging()
    signals.setup()
    return asyncio.run(run_forever())


async def async_status() -> None:
    worker_per_process: int = config.STREAM_WORKERS_PER_PROCESS
    process_count: int = config.STREAM_PROCESSES
    worker_count: int = worker_per_process * process_count

    redis_stream = utils.create_aredis_for_stream()
    stream_selector = StreamSelector(redis_stream, 0, worker_count)

    def sorter(item):
        stream, score = item
        return stream_selector.get_worker_id_for(stream)

    streams = sorted(
        await redis_stream.zrangebyscore("streams", min=0, max="+inf", withscores=True),
        key=sorter,
    )

    for worker_id, streams_by_worker in itertools.groupby(streams, key=sorter):
        for stream, score in streams_by_worker:
            owner = stream.split(b"~")[1]
            date = datetime.datetime.utcfromtimestamp(score).isoformat(" ", "seconds")
            items = await redis_stream.xlen(stream)
            print(f"{{{worker_id:02}}} [{date}] {owner.decode()}: {items} events")


def status() -> None:
    asyncio.run(async_status())


async def async_reschedule_now() -> int:
    parser = argparse.ArgumentParser(description="Rescheduler for Mergify")
    parser.add_argument("org", help="Organization")
    args = parser.parse_args()

    redis = utils.create_aredis_for_stream()
    streams = await redis.zrangebyscore("streams", min=0, max="+inf")
    expected_stream = f"stream~{args.org.lower()}~"
    for stream in streams:
        if stream.decode().lower().startswith(expected_stream):
            scheduled_at = date.utcnow()
            score = scheduled_at.timestamp()
            transaction = await redis.pipeline()
            await transaction.hdel("attempts", stream)
            await transaction.zadd("streams", **{stream.decode(): score})
            # NOTE(sileht): Do we need to cleanup the per PR attempt?
            # await transaction.hdel("attempts", attempts_key)
            await transaction.execute()
            return 0
    else:
        print(f"Stream for {args.org} not found")
        return 1


def reschedule_now() -> int:
    return asyncio.run(async_reschedule_now())
