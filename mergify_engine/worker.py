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
import logging
import os
import signal
import threading
import time
import typing

import aredis
import daiquiri
from datadog import statsd
import msgpack
import tenacity

from mergify_engine import config
from mergify_engine import context
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import logs
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http


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


class IgnoredException(Exception):
    pass


@dataclasses.dataclass
class PullRetry(Exception):
    attempts: int


class MaxPullRetry(PullRetry):
    pass


@dataclasses.dataclass
class StreamRetry(Exception):
    stream_name: str
    attempts: int
    retry_at: datetime.datetime


class StreamUnused(Exception):
    stream_name: str


T_MessagePayload = typing.NewType("T_MessagePayload", typing.Dict[bytes, bytes])
T_MessageID = typing.NewType("T_MessageID", str)


class T_PayloadEvent(typing.TypedDict):
    owner: str
    repo: str
    pull_number: int
    source: context.T_PayloadEventSource


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(aredis.ConnectionError),
)
async def push(
    redis: aredis.StrictRedis,
    owner_id: github_types.GitHubAccountIdType,
    owner: github_types.GitHubLogin,
    repo: github_types.GitHubRepositoryName,
    pull_number: typing.Optional[int],
    event_type: github_types.GitHubEventType,
    data: github_types.GitHubEvent,
) -> typing.Tuple[T_MessageID, T_MessagePayload]:
    stream_name = f"stream~{owner}~{owner_id}"
    scheduled_at = utils.utcnow() + datetime.timedelta(seconds=WORKER_PROCESSING_DELAY)
    score = scheduled_at.timestamp()
    transaction = await redis.pipeline()
    # NOTE(sileht): Add this event to the pull request stream
    payload = T_MessagePayload(
        {
            b"event": msgpack.packb(
                {
                    "owner_id": owner_id,
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


async def get_pull_for_engine(
    owner: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: int,
    logger: logging.LoggerAdapter,
) -> typing.Optional[
    typing.Tuple[subscription.Subscription, github_types.GitHubPullRequest]
]:
    async with await github.aget_client(owner) as client:
        try:
            pull = await client.item(f"/repos/{owner}/{repo_name}/pulls/{pull_number}")
        except http.HTTPNotFound:
            # NOTE(sileht): Don't fail if we received even on repo/pull that doesn't exists anymore
            logger.debug("pull request doesn't exists, skipping it")
            return None

        if client.auth.owner_id is None:
            raise RuntimeError("owner_id is None")

        sub = await subscription.Subscription.get_subscription(client.auth.owner_id)

        return sub, pull


def run_engine(
    owner: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: int,
    sources: typing.List[context.T_PayloadEventSource],
) -> None:
    logger = daiquiri.getLogger(
        __name__, gh_repo=repo_name, gh_owner=owner, gh_pull=pull_number
    )
    logger.debug("engine in thread start")
    try:
        result = asyncio.run(get_pull_for_engine(owner, repo_name, pull_number, logger))
        if result:
            subscription, pull = result
            with github.get_client(owner) as client:
                engine.run(client, pull, subscription, sources)
    finally:
        logger.debug("engine in thread end")


class ThreadRunner(threading.Thread):
    """This thread propagate exception to main thread."""

    def __init__(self):
        super().__init__(daemon=True)
        self._method = None
        self._args = None
        self._kwargs = None
        self._result = None
        self._exception = None

        self._stopping = threading.Event()
        self._process = threading.Event()
        self.start()

    async def exec(self, method, *args, **kwargs):
        self._method = method
        self._args = args
        self._kwargs = kwargs
        self._result = None
        self._exception = None

        self._process.set()
        while self._process.is_set():
            await asyncio.sleep(0.01)

        if self._stopping.is_set():
            return

        if self._exception:
            raise self._exception

        return self._result

    def close(self):
        self._stopping.set()
        self._process.set()
        self.join()

    def run(self):
        while not self._stopping.is_set():
            self._process.wait()
            if self._stopping.is_set():
                self._process.clear()
                return

            try:
                self._result = self._method(*self._args, **self._kwargs)
            except BaseException as e:
                self._exception = e
            finally:
                self._process.clear()


PullsToConsume = typing.NewType(
    "PullsToConsume",
    collections.OrderedDict[
        typing.Tuple[str, str, int],
        typing.Tuple[
            typing.List[T_MessageID], typing.List[context.T_PayloadEventSource]
        ],
    ],
)


@dataclasses.dataclass
class StreamSelector:
    redis: aredis.StrictRedis
    worker_id: int
    worker_count: int

    def get_worker_id_for(self, stream: bytes) -> int:
        return int(hashlib.md5(stream).hexdigest(), 16) % self.worker_count

    def _is_stream_for_me(self, stream: bytes) -> bool:
        return self.get_worker_id_for(stream) == self.worker_id

    async def next_stream(self):
        now = time.time()
        for stream in await self.redis.zrangebyscore(
            "streams",
            min=0,
            max=now,
        ):
            if self._is_stream_for_me(stream):
                statsd.increment(
                    "engine.streams.selected", tags=[f"worker_id:{self.worker_id}"]
                )
                return stream.decode()


@dataclasses.dataclass
class StreamProcessor:
    redis: aredis.StrictRedis
    _thread: ThreadRunner = dataclasses.field(init=False, default_factory=ThreadRunner)

    def close(self):
        self._thread.close()

    @contextlib.asynccontextmanager
    async def _translate_exception_to_retries(
        self,
        stream_name: str,
        attempts_key: typing.Optional[str] = None,
    ) -> typing.AsyncIterator[None]:
        try:
            yield
        except Exception as e:
            if isinstance(e, exceptions.MergeableStateUnknown) and attempts_key:
                attempts = await self.redis.hincrby("attempts", attempts_key)
                if attempts < MAX_RETRIES:
                    raise PullRetry(attempts) from e
                else:
                    await self.redis.hdel("attempts", attempts_key)
                    raise MaxPullRetry(attempts) from e

            if isinstance(e, exceptions.MergifyNotInstalled):
                if attempts_key:
                    await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", stream_name)
                raise StreamUnused(stream_name)

            if isinstance(e, github.TooManyPages):
                # TODO(sileht): Ideally this should be catcher earlier to post an
                # appropriate check-runs to inform user the PR is too big to be handled
                # by Mergify, but this need a bit of refactory to do it, so in the
                # meantimes...
                if attempts_key:
                    await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", stream_name)
                raise IgnoredException()

            if exceptions.should_be_ignored(e):
                if attempts_key:
                    await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", stream_name)
                raise IgnoredException()

            if isinstance(e, exceptions.RateLimited):
                retry_at = utils.utcnow() + e.countdown
                score = retry_at.timestamp()
                if attempts_key:
                    await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", stream_name)
                await self.redis.zaddoption("streams", "XX", **{stream_name: score})
                raise StreamRetry(stream_name, 0, retry_at)

            backoff = exceptions.need_retry(e)
            if backoff is None:
                # NOTE(sileht): This is our fault, so retry until we fix the bug but
                # without increasing the attempts
                raise

            attempts = await self.redis.hincrby("attempts", stream_name)
            retry_in = 3 ** min(attempts, 3) * backoff
            retry_at = utils.utcnow() + retry_in
            score = retry_at.timestamp()
            await self.redis.zaddoption("streams", "XX", **{stream_name: score})
            raise StreamRetry(stream_name, attempts, retry_at)

    async def consume(self, stream_name: str) -> None:
        owner = github_types.GitHubLogin(stream_name.split("~")[1])

        try:
            pulls = await self._extract_pulls_from_stream(stream_name)
            await self._consume_pulls(stream_name, pulls)
        except StreamUnused:
            LOG.info("unused stream, dropping it", gh_owner=owner, exc_info=True)
            await self.redis.delete(stream_name)
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
                gh_owner=owner,
                exc_info=True,
            )
            return
        except vcr_errors_CannotOverwriteExistingCassetteException:
            messages = await self.redis.xrange(
                stream_name, count=config.STREAM_MAX_BATCH
            )
            for message_id, message in messages:
                LOG.info(msgpack.unpackb(message[b"event"], raw=False))
                await self.redis.execute_command("XDEL", stream_name, message_id)

        except Exception:
            # Ignore it, it will retried later
            LOG.error("failed to process stream", gh_owner=owner, exc_info=True)

        LOG.debug("cleanup stream start", stream_name=stream_name)
        await self.redis.eval(
            self.ATOMIC_CLEAN_STREAM_SCRIPT, 1, stream_name.encode(), time.time()
        )
        LOG.debug("cleanup stream end", stream_name=stream_name)

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

    async def _extract_pulls_from_stream(self, stream_name: str) -> PullsToConsume:
        messages: typing.List[
            typing.Tuple[T_MessageID, T_MessagePayload]
        ] = await self.redis.xrange(stream_name, count=config.STREAM_MAX_BATCH)
        LOG.debug("read stream", stream_name=stream_name, messages_count=len(messages))
        statsd.histogram("engine.streams.size", len(messages))
        statsd.gauge("engine.streams.max_size", config.STREAM_MAX_BATCH)

        opened_pulls_by_repo: typing.Dict[
            typing.Tuple[github_types.GitHubLogin, github_types.GitHubRepositoryName],
            typing.List[github_types.GitHubPullRequest],
        ] = {}

        # Groups stream by pull request
        pulls: PullsToConsume = PullsToConsume(collections.OrderedDict())
        for message_id, message in messages:
            data = msgpack.unpackb(message[b"event"], raw=False)
            owner = github_types.GitHubLogin(data["owner"])
            repo = github_types.GitHubRepositoryName(data["repo"])
            source = typing.cast(context.T_PayloadEventSource, data["source"])

            if data["pull_number"] is not None:
                key = (owner, repo, data["pull_number"])
                group = pulls.setdefault(key, ([], []))
                group[0].append(message_id)
                group[1].append(source)
            else:
                logger = daiquiri.getLogger(
                    __name__, gh_repo=repo, gh_owner=owner, source=source
                )
                if (owner, repo) not in opened_pulls_by_repo:
                    try:
                        async with self._translate_exception_to_retries(stream_name):
                            async with await github.aget_client(owner) as client:
                                opened_pulls_by_repo[(owner, repo)] = [
                                    p
                                    async for p in client.items(
                                        f"/repos/{client.auth.owner}/{repo}/pulls"
                                    )
                                ]
                    except IgnoredException:
                        opened_pulls_by_repo[(owner, repo)] = []

                if client.auth.owner_id is None:
                    raise RuntimeError("owner_id is None")

                converted_messages = await self._convert_event_to_messages(
                    client.auth.owner_id,
                    owner,
                    repo,
                    source,
                    opened_pulls_by_repo[(owner, repo)],
                )

                logger.debug("event unpacked into %s messages", len(converted_messages))
                messages.extend(converted_messages)
                deleted = await self.redis.xdel(stream_name, message_id)
                if deleted != 1:
                    # FIXME(sileht): During shutdown, heroku may have already started
                    # another worker that have already take the lead of this stream_name
                    # This can create duplicate events in the streams but that should not
                    # be a big deal as the engine will not been ran by the worker that's
                    # shutdowning.
                    contents = await self.redis.xrange(
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
        owner_id: github_types.GitHubAccountIdType,
        owner_login: github_types.GitHubLogin,
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
            owner_login,
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
                    self.redis,
                    owner_id,
                    owner_login,
                    repo_name,
                    pull_number,
                    source["event_type"],
                    source["data"],
                )
            )
        return messages

    async def _consume_pulls(self, stream_name: str, pulls: PullsToConsume) -> None:
        LOG.debug("stream contains %d pulls", len(pulls), stream_name=stream_name)
        for (owner, repo, pull_number), (message_ids, sources) in pulls.items():

            statsd.histogram("engine.streams.batch-size", len(sources))
            for source in sources:
                if "timestamp" in source:
                    statsd.histogram(
                        "engine.streams.events.latency",
                        (
                            datetime.datetime.utcnow()
                            - datetime.datetime.fromisoformat(source["timestamp"])
                        ).total_seconds(),
                    )

            logger = daiquiri.getLogger(
                __name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number
            )

            attempts_key = f"pull~{owner}~{repo}~{pull_number}"
            try:
                async with self._translate_exception_to_retries(
                    stream_name, attempts_key
                ):
                    await self._thread.exec(
                        run_engine,
                        owner,
                        repo,
                        pull_number,
                        sources,
                    )
                await self.redis.hdel("attempts", attempts_key)
                await self.redis.execute_command("XDEL", stream_name, *message_ids)
            except IgnoredException:
                await self.redis.execute_command("XDEL", stream_name, *message_ids)
                logger.debug("failed to process pull request, ignoring", exc_info=True)
            except MaxPullRetry as e:
                await self.redis.execute_command("XDEL", stream_name, *message_ids)
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
    shutdown_timeout: float = 25
    worker_per_process: int = config.STREAM_WORKERS_PER_PROCESS
    process_count: int = config.STREAM_PROCESSES
    process_index: int = dataclasses.field(default_factory=get_process_index_from_env)
    enabled_services: typing.Set[
        typing.Literal["stream", "stream-monitoring"]
    ] = dataclasses.field(default_factory=lambda: {"stream", "stream-monitoring"})

    _redis: aredis.StrictRedis = dataclasses.field(init=False, default=None)

    _loop: asyncio.AbstractEventLoop = dataclasses.field(
        init=False, default_factory=asyncio.get_running_loop
    )
    _stopping: asyncio.Event = dataclasses.field(
        init=False, default_factory=asyncio.Event
    )
    _tombstone: asyncio.Event = dataclasses.field(
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
        # NOTE(sileht): This task must never fail, we don't want to write code to
        # reap/clean/respawn them
        stream_processor = StreamProcessor(self._redis)
        stream_selector = StreamSelector(self._redis, worker_id, self.worker_count)

        while not self._stopping.is_set():
            try:
                stream_name = await stream_selector.next_stream()
                if stream_name:
                    LOG.debug("worker %s take stream: %s", worker_id, stream_name)
                    try:
                        with statsd.timed("engine.stream.consume.time"):
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
                # NOTE(sileht): We don't wait for the thread and just return, the thread
                # will be killed when the program exits.
                LOG.debug("worker %s killed", worker_id)
                return
            except Exception:
                LOG.error("worker %s fail, sleeping a bit", worker_id, exc_info=True)
                await self._sleep_or_stop()

        stream_processor.close()
        LOG.debug("worker %s exited", worker_id)

    async def _sleep_or_stop(self, timeout=None):
        if timeout is None:
            timeout = self.idle_sleep_time
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def monitoring_task(self) -> None:
        while not self._stopping.is_set():
            try:
                now = time.time()
                streams = await self._redis.zrangebyscore(
                    "streams",
                    min=0,
                    max=now,
                    withscores=True,
                )
                # NOTE(sileht): The latency may not be exact with the next StreamSelector
                # based on hash+modulo
                if len(streams) > self.worker_count:
                    latency = now - streams[self.worker_count][1]
                    statsd.timing("engine.streams.latency", latency)
                else:
                    statsd.timing("engine.streams.latency", 0)

                statsd.gauge("engine.streams.backlog", len(streams))
                statsd.gauge("engine.workers.count", self.worker_count)
                statsd.gauge("engine.processes.count", self.process_count)
                statsd.gauge(
                    "engine.workers-per-process.count", self.worker_per_process
                )
            except asyncio.CancelledError:
                LOG.debug("monitoring task killed")
                return
            except Exception:
                LOG.error("monitoring task failed", exc_info=True)

            await self._sleep_or_stop(60)

    def get_worker_ids(self):
        return list(
            range(
                self.process_index * self.worker_per_process,
                (self.process_index + 1) * self.worker_per_process,
            )
        )

    async def _run(self):
        self._stopping.clear()

        self._redis = await utils.create_aredis_for_stream()

        if "stream" in self.enabled_services:
            worker_ids = self.get_worker_ids()
            for worker_id in worker_ids:
                self._worker_tasks.append(
                    asyncio.create_task(self.stream_worker_task(worker_id))
                )
            LOG.info("workers %s started", ", ".join(map(str, worker_ids)))

        if "stream-monitoring" in self.enabled_services:
            self._stream_monitoring_task = asyncio.create_task(self.monitoring_task())

    async def _shutdown(self):
        worker_ids = self.get_worker_ids()
        LOG.info("wait for workers %s to exit", ", ".join(map(str, worker_ids)))

        await self._start_task

        tasks = []
        if "stream" in self.enabled_services:
            tasks.extend(self._worker_tasks)
        if "stream-monitoring" in self.enabled_services:
            tasks.append(self._stream_monitoring_task)

        done, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
        if pending:
            for task in pending:
                task.cancel(msg="shutdown")
            await asyncio.wait(pending)

        self._worker_tasks = []

        if self._redis:
            self._redis.connection_pool.max_idle_time = 0
            self._redis.connection_pool.disconnect()
            self._redis = None
            await utils.stop_pending_aredis_tasks()

        self._tombstone.set()
        LOG.debug("exiting")

    def start(self):
        self._start_task = asyncio.create_task(self._run())

    def stop(self):
        self._stopping.set()
        self._stop_task = asyncio.create_task(self._shutdown())

    async def wait_shutdown_complete(self):
        await self._tombstone.wait()
        await asyncio.wait([self._stop_task])

    def stop_with_signal(self, signame):
        if not self._stopping.is_set():
            LOG.info("got signal %s: cleanly shutdown workers", signame)
            self.stop()
        else:
            LOG.info("got signal %s: ignoring, shutdown already in process", signame)

    def setup_signals(self):
        for signame in ("SIGINT", "SIGTERM"):
            self._loop.add_signal_handler(
                getattr(signal, signame),
                functools.partial(self.stop_with_signal, signame),
            )


async def run_forever() -> None:
    worker = Worker()
    worker.setup_signals()
    worker.start()
    await worker.wait_shutdown_complete()
    LOG.info("Exiting...")


def main() -> None:
    logs.setup_logging()
    return asyncio.run(run_forever())


async def async_status() -> None:
    worker_per_process: int = config.STREAM_WORKERS_PER_PROCESS
    process_count: int = config.STREAM_PROCESSES
    worker_count: int = worker_per_process * process_count

    redis = await utils.create_aredis_for_stream()
    stream_selector = StreamSelector(redis, 0, worker_count)

    def sorter(item):
        stream, score = item
        return stream_selector.get_worker_id_for(stream)

    streams = sorted(
        await redis.zrangebyscore("streams", min=0, max="+inf", withscores=True),
        key=sorter,
    )

    for worker_id, streams_by_worker in itertools.groupby(streams, key=sorter):
        for stream, score in streams_by_worker:
            owner = stream.split(b"~")[1]
            date = datetime.datetime.utcfromtimestamp(score).isoformat(" ", "seconds")
            items = await redis.xlen(stream)
            print(f"{{{worker_id:02}}} [{date}] {owner.decode()}: {items} events")


def status() -> None:
    asyncio.run(async_status())


async def async_reschedule_now() -> int:
    parser = argparse.ArgumentParser(description="Rescheduler for Mergify")
    parser.add_argument("org", help="Organization")
    args = parser.parse_args()

    redis = await utils.create_aredis_for_stream()
    streams = await redis.zrangebyscore("streams", min=0, max="+inf")
    expected_stream = f"stream~{args.org.lower()}~"
    for stream in streams:
        if stream.decode().lower().startswith(expected_stream):
            scheduled_at = utils.utcnow()
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
