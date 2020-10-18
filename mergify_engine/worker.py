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
import collections
import contextlib
import dataclasses
import datetime
import functools
import signal
import threading
import time
from typing import Any
from typing import List
from typing import Set

import daiquiri
from datadog import statsd
import msgpack

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import github_events
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


async def push(redis, owner, repo, pull_number, event_type, data):
    stream_name = f"stream~{owner}"
    scheduled_at = utils.utcnow() + datetime.timedelta(seconds=WORKER_PROCESSING_DELAY)
    score = scheduled_at.timestamp()
    transaction = await redis.pipeline()
    # NOTE(sileht): Add this event to the pull request stream
    payload = {
        b"event": msgpack.packb(
            {
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

    await transaction.xadd(stream_name, payload)
    # NOTE(sileht): Add pull request stream to process to the list, only if it
    # does not exists, to not update the score(date)
    await transaction.zaddoption("streams", "NX", **{stream_name: score})
    ret = await transaction.execute()
    LOG.debug(
        "pushed to worker",
        gh_owner=owner,
        gh_repo=repo,
        gh_pull=pull_number,
        event_type=event_type,
    )
    return (ret[0], payload)


async def get_pull_for_engine(owner, repo, pull_number, logger):
    async with await github.aget_client(owner) as client:
        try:
            pull = await client.item(f"/repos/{owner}/{repo}/pulls/{pull_number}")
        except http.HTTPNotFound:
            # NOTE(sileht): Don't fail if we received even on repo/pull that doesn't exists anymore
            logger.debug("pull request doesn't exists, skipping it")
            return

        sub = await subscription.Subscription.get_subscription(client.auth.owner_id)

        return sub, pull


def run_engine(owner, repo, pull_number, sources):
    logger = daiquiri.getLogger(
        __name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number
    )
    logger.debug("engine in thread start")
    try:
        result = asyncio.run(get_pull_for_engine(owner, repo, pull_number, logger))
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


@dataclasses.dataclass
class StreamSelector:
    worker_count: int
    redis: Any

    _pending_streams: Set = dataclasses.field(init=False, default_factory=set)

    @contextlib.asynccontextmanager
    async def next_stream(self):
        # TODO(sileht): We can cache locally the result as the order is not going to
        # change, and if it changes we don't care
        # NOTE(sileht): We get the numbers we need to have one per worker, then remove
        # streams already handled by other workers and keep the remaining one.
        now = time.time()
        streams = [
            s
            for s in await self.redis.zrangebyscore(
                "streams",
                min=0,
                max=now,
                start=0,
                num=self.worker_count * 2,
            )
            if s not in self._pending_streams
        ]
        if streams:
            self._pending_streams.add(streams[0])
            try:
                yield streams[0].decode()
            finally:
                self._pending_streams.remove(streams[0])
        else:
            yield


@dataclasses.dataclass
class StreamProcessor:
    redis: Any
    _thread: ThreadRunner = dataclasses.field(init=False, default_factory=ThreadRunner)

    def close(self):
        self._thread.close()

    async def _translate_exception_to_retries(
        self,
        e,
        stream_name,
        attempts_key=None,
    ):
        if isinstance(e, exceptions.MergifyNotInstalled):
            if attempts_key:
                await self.redis.hdel("attempts", attempts_key)
            await self.redis.hdel("attempts", stream_name)
            raise StreamUnused(stream_name) from e

        if isinstance(e, github.TooManyPages):
            # TODO(sileht): Ideally this should be catcher earlier to post an
            # appropriate check-runs to inform user the PR is too big to be handled
            # by Mergify, but this need a bit of refactory to do it, so in the
            # meantimes...
            if attempts_key:
                await self.redis.hdel("attempts", attempts_key)
            await self.redis.hdel("attempts", stream_name)
            raise IgnoredException() from e

        if exceptions.should_be_ignored(e):
            if attempts_key:
                await self.redis.hdel("attempts", attempts_key)
            await self.redis.hdel("attempts", stream_name)
            raise IgnoredException() from e

        if isinstance(e, exceptions.RateLimited):
            retry_at = utils.utcnow() + datetime.timedelta(seconds=e.countdown)
            score = retry_at.timestamp()
            if attempts_key:
                await self.redis.hdel("attempts", attempts_key)
            await self.redis.hdel("attempts", stream_name)
            await self.redis.zaddoption("streams", "XX", **{stream_name: score})
            raise StreamRetry(stream_name, 0, retry_at) from e

        backoff = exceptions.need_retry(e)
        if backoff is None:
            # NOTE(sileht): This is our fault, so retry until we fix the bug but
            # without increasing the attempts
            raise

        attempts = await self.redis.hincrby("attempts", stream_name)
        retry_in = 3 ** min(attempts, 3) * backoff
        retry_at = utils.utcnow() + datetime.timedelta(seconds=retry_in)
        score = retry_at.timestamp()
        await self.redis.zaddoption("streams", "XX", **{stream_name: score})
        raise StreamRetry(stream_name, attempts, retry_at) from e

    async def _run_engine_and_translate_exception_to_retries(
        self, stream_name, owner, repo, pull_number, sources
    ):
        attempts_key = f"pull~{owner}~{repo}~{pull_number}"
        try:
            await self._thread.exec(
                run_engine,
                owner,
                repo,
                pull_number,
                sources,
            )
            await self.redis.hdel("attempts", attempts_key)
        # Translate in more understandable exception
        except exceptions.MergeableStateUnknown as e:
            attempts = await self.redis.hincrby("attempts", attempts_key)
            if attempts < MAX_RETRIES:
                raise PullRetry(attempts) from e
            else:
                await self.redis.hdel("attempts", attempts_key)
                raise MaxPullRetry(attempts) from e

        except Exception as e:
            await self._translate_exception_to_retries(e, stream_name, attempts_key)

    async def consume(self, stream_name):
        owner = stream_name.split("~", 1)[1]

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

    async def _extract_pulls_from_stream(self, stream_name):
        messages = await self.redis.xrange(stream_name, count=config.STREAM_MAX_BATCH)
        LOG.debug("read stream", stream_name=stream_name, messages_count=len(messages))
        statsd.histogram("engine.streams.size", len(messages))

        # Groups stream by pull request
        pulls = collections.OrderedDict()
        for message_id, message in messages:
            data = msgpack.unpackb(message[b"event"], raw=False)
            owner = data["owner"]
            repo = data["repo"]
            source = data["source"]

            if data["pull_number"] is not None:
                key = (owner, repo, data["pull_number"])
                group = pulls.setdefault(key, ([], []))
                group[0].append(message_id)
                group[1].append(source)
            else:
                logger = daiquiri.getLogger(
                    __name__, gh_repo=repo, gh_owner=owner, source=source
                )
                logger.debug("unpacking event")
                try:
                    converted_messages = await self._convert_event_to_messages(
                        stream_name, owner, repo, source
                    )
                except IgnoredException:
                    converted_messages = []
                    logger.debug("ignored error", exc_info=True)
                except StreamRetry:
                    raise
                except StreamUnused:
                    raise
                except Exception:
                    # Ignore it, it will retried later
                    logger.error("failed to process incomplete event", exc_info=True)
                    continue

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

    async def _convert_event_to_messages(self, stream_name, owner, repo, source):
        # NOTE(sileht): the event is incomplete (push, refresh, checks, status)
        # So we get missing pull numbers, add them to the stream to
        # handle retry later, add them to message to run engine on them now,
        # and delete the current message_id as we have unpack this incomplete event into
        # multiple complete event
        try:
            async with await github.aget_client(owner) as client:
                pull_numbers = await github_events.extract_pull_numbers_from_event(
                    client,
                    repo,
                    source["event_type"],
                    source["data"],
                )
        except Exception as e:
            await self._translate_exception_to_retries(e, stream_name)

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
                    owner,
                    repo,
                    pull_number,
                    source["event_type"],
                    source["data"],
                )
            )
        return messages

    async def _consume_pulls(self, stream_name, pulls):
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

            try:
                logger.debug("engine start with %s sources", len(sources))
                start = time.monotonic()
                await self._run_engine_and_translate_exception_to_retries(
                    stream_name, owner, repo, pull_number, sources
                )
                await self.redis.execute_command("XDEL", stream_name, *message_ids)
                end = time.monotonic()
                logger.debug("engine finished in %s sec", end - start)
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


@dataclasses.dataclass
class Worker:
    idle_sleep_time: float = 0.42
    worker_count: int = config.STREAM_WORKERS
    enabled_services: List[str] = dataclasses.field(
        default_factory=lambda: ["stream", "stream-monitoring"]
    )

    _redis: Any = dataclasses.field(init=False, default=None)

    _loop: Any = dataclasses.field(init=False, default_factory=asyncio.get_running_loop)
    _stopping: Any = dataclasses.field(init=False, default_factory=asyncio.Event)
    _tombstone: Any = dataclasses.field(init=False, default_factory=asyncio.Event)

    _worker_tasks: List = dataclasses.field(init=False, default_factory=list)
    _stream_monitoring_task: Any = dataclasses.field(init=False, default=None)

    async def stream_worker_task(self, worker_id):
        # NOTE(sileht): This task must never fail, we don't want to write code to
        # reap/clean/respawn them
        stream_processor = StreamProcessor(self._redis)

        while not self._stopping.is_set():
            try:
                async with self._stream_selector.next_stream() as stream_name:
                    if stream_name:
                        LOG.debug("worker %d take stream: %s", worker_id, stream_name)
                        try:
                            await stream_processor.consume(stream_name)
                        finally:
                            LOG.debug(
                                "worker %d release stream: %s",
                                worker_id,
                                stream_name,
                            )
                    else:
                        LOG.debug(
                            "worker %d has nothing to do, sleeping a bit", worker_id
                        )
                        await self._sleep_or_stop()
            except Exception:
                LOG.error("worker %d fail, sleeping a bit", worker_id, exc_info=True)
                await self._sleep_or_stop()

        stream_processor.close()
        LOG.debug("worker %d exited", worker_id)

    async def _sleep_or_stop(self, timeout=None):
        if timeout is None:
            timeout = self.idle_sleep_time
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def monitoring_task(self):
        while not self._stopping.is_set():
            try:
                now = time.time()
                streams = await self._redis.zrangebyscore(
                    "streams",
                    min=0,
                    max=now,
                    start=self.worker_count,
                    num=1,
                    withscores=True,
                )
                if streams:
                    latency = now - streams[0][1]
                    statsd.timing("engine.streams.latency", latency)
                else:
                    statsd.timing("engine.streams.latency", 0)

                statsd.gauge("engine.workers.count", self.worker_count)
            except Exception:
                LOG.warning("monitoring task failed", exc_info=True)

            await self._sleep_or_stop(60)

    async def _run(self):
        self._stopping.clear()

        self._redis = await utils.create_aredis_for_stream()
        self._stream_selector = StreamSelector(self.worker_count, self._redis)

        if "stream" in self.enabled_services:
            for i in range(self.worker_count):
                self._worker_tasks.append(
                    asyncio.create_task(self.stream_worker_task(i))
                )

        if "stream-monitoring" in self.enabled_services:
            self._stream_monitoring_task = asyncio.create_task(self.monitoring_task())

        LOG.debug("%d workers spawned", self.worker_count)

    async def _shutdown(self):
        LOG.debug("wait for workers to exit")
        self._stopping.set()

        await self._start_task

        if self._stream_monitoring_task:
            await self._stream_monitoring_task

        if self._worker_tasks:
            await asyncio.wait(self._worker_tasks)
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
        self._stop_task = asyncio.create_task(self._shutdown())

    async def wait_shutdown_complete(self):
        await self._tombstone.wait()
        await asyncio.wait([self._stop_task])

    def stop_with_signal(self, signame):
        if not self._stopping.is_set():
            LOG.info("got signal %s: cleanly shutdown worker", signame)
            self.stop()
        else:
            LOG.info("got signal %s again: exiting now...", signame)
            self._loop.stop()

    def setup_signals(self):
        for signame in ("SIGINT", "SIGTERM"):
            self._loop.add_signal_handler(
                getattr(signal, signame),
                functools.partial(self.stop_with_signal, signame),
            )


async def run_forever():
    worker = Worker()
    worker.setup_signals()
    worker.start()
    await worker.wait_shutdown_complete()
    LOG.info("Exiting...")


def main():
    logs.setup_logging()
    asyncio.run(run_forever())


async def async_status():
    redis = await utils.create_aredis_for_stream()
    streams = await redis.zrangebyscore("streams", min=0, max="+inf", withscores=True)

    for stream, score in streams:
        owner = stream.split(b"~")[1]
        date = datetime.datetime.utcfromtimestamp(score).isoformat(" ", "seconds")
        items = await redis.xlen(stream)
        print(f"[{date}] {owner}: {items} events")


def status():
    asyncio.run(async_status())
