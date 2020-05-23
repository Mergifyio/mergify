# debug
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

from datadog import statsd
import httpx
import msgpack
import uvloop

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import logs
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.clients import github


try:
    import vcr

    vcr_errors_CannotOverwriteExistingCassetteException = (
        vcr.errors.CannotOverwriteExistingCassetteException
    )
except ImportError:

    class vcr_errors_CannotOverwriteExistingCassetteException(Exception):
        pass


LOG = logs.getLogger(__name__)


MAX_RETRIES = 3
WORKER_PROCESSING_DELAY = 30


class IgnoredException(Exception):
    pass


@dataclasses.dataclass
class PullRetry(Exception):
    attempts: int


class MaxPullRetry(PullRetry):
    pass


@dataclasses.dataclass
class StreamRetry(Exception):
    attempts: int
    retry_at: datetime.datetime


async def push(redis, installation_id, owner, repo, pull_number, event_type, data):
    stream_name = f"stream~{installation_id}"
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
                "source": {"event_type": event_type, "data": data},
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


def run_engine(installation, owner, repo, pull_number, sources):
    logger = logs.getLogger(__name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number)
    logger.debug("engine in thread start")
    try:
        sync_redis = utils.get_redis_for_cache()
        subscription = sub_utils.get_subscription(sync_redis, installation["id"])
        logger.debug("engine get installation")
        with github.get_client(owner, repo, installation) as client:
            try:
                pull = client.item(f"pulls/{pull_number}")
            except httpx.HTTPClientSideError as e:
                if e.status_code == 404:
                    logger.debug("pull request doesn't exists, skipping it")
                    return
                raise

            if (
                pull["base"]["repo"]["private"]
                and not subscription["subscription_active"]
            ):
                logger.debug(
                    "pull request on private private repository without subscription, skipping it"
                )
                return

            engine.run(client, pull, sources)
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

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
                "streams", min=0, max=now, start=0, num=self.worker_count * 2,
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
        self, e, installation_id, attempts_key=None,
    ):
        stream_name = f"stream~{installation_id}"

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
            retry_at = utils.utcnow() + datetime.timedelta(seconds=e.countdown)
            score = retry_at.timestamp()
            if attempts_key:
                await self.redis.hdel("attempts", attempts_key)
            await self.redis.hdel("attempts", stream_name)
            await self.redis.zaddoption("streams", "XX", **{stream_name: score})
            raise StreamRetry(0, retry_at) from e

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
        raise StreamRetry(attempts, retry_at) from e

    async def _run_engine_and_translate_exception_to_retries(
        self, installation, owner, repo, pull_number, sources
    ):
        attempts_key = f"pull~{installation['id']}~{owner}~{repo}~{pull_number}"
        try:
            await self._thread.exec(
                run_engine, installation, owner, repo, pull_number, sources,
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
            await self._translate_exception_to_retries(
                e, installation["id"], attempts_key
            )

    async def get_installation(self, stream_name):
        installation_id = int(stream_name.split("~")[1])
        try:
            return await self._thread.exec(
                github.get_installation_by_id, installation_id
            )
        except Exception as e:
            await self._translate_exception_to_retries(e, installation_id)

    async def consume(self, stream_name):
        installation = None
        try:
            installation = await self.get_installation(stream_name)
            pulls = await self._extract_pulls_from_stream(stream_name, installation)
            await self._consume_pulls(stream_name, installation, pulls)
        except exceptions.MergifyNotInstalled:
            LOG.debug(
                "mergify not installed",
                gh_owner=installation["account"]["login"] if installation else None,
                exc_info=True,
            )
            await self.redis.delete(stream_name)
        except StreamRetry as e:
            LOG.info(
                "failed to process stream, retrying",
                gh_owner=installation["account"]["login"] if installation else None,
                attempts=e.attempts,
                retry_at=e.retry_at,
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
            LOG.error(
                "failed to process stream",
                gh_owner=installation["account"]["login"] if installation else None,
                exc_info=True,
            )

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

    async def _extract_pulls_from_stream(self, stream_name, installation):
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
                logger = logs.getLogger(__name__, gh_repo=repo, gh_owner=owner)
                try:
                    messages.extend(
                        await self._convert_event_to_messages(
                            stream_name, installation, owner, repo, source
                        )
                    )
                except IgnoredException:
                    logger.debug("ignored error", exc_info=True)
                except StreamRetry:
                    raise
                except Exception:
                    # Ignore it, it will retried later
                    logger.error("failed to process incomplete event", exc_info=True)
                    continue

                await self.redis.xdel(stream_name, message_id)
        return pulls

    async def _convert_event_to_messages(
        self, stream_name, installation, owner, repo, source
    ):
        # NOTE(sileht): the event is incomplete (push, refresh, checks, status)
        # So we get missing pull numbers, add them to the stream to
        # handle retry later, add them to message to run engine on them now,
        # and delete the current message_id as we have unpack this incomplete event into
        # multiple complete event
        try:
            pull_numbers = await self._thread.exec(
                github_events.extract_pull_numbers_from_event,
                installation,
                owner,
                repo,
                source["event_type"],
                source["data"],
            )
        except Exception as e:
            await self._translate_exception_to_retries(e, installation["id"])

        messages = []
        for pull_number in pull_numbers:
            messages.append(
                await push(
                    self.redis,
                    installation["id"],
                    owner,
                    repo,
                    pull_number,
                    source["event_type"],
                    source["data"],
                )
            )
        return messages

    async def _consume_pulls(self, stream_name, installation, pulls):
        LOG.debug("stream contains %d pulls", len(pulls), stream_name=stream_name)
        for (owner, repo, pull_number), (message_ids, sources) in pulls.items():
            statsd.histogram("engine.streams.batch-size", len(sources))
            logger = logs.getLogger(
                __name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number
            )

            try:
                logger.debug("engine start with %s sources", len(sources))
                start = time.monotonic()
                await self._run_engine_and_translate_exception_to_retries(
                    installation, owner, repo, pull_number, sources
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
            except Exception:
                # Ignore it, it will retried later
                logger.error("failed to process pull request", exc_info=True)


@dataclasses.dataclass
class Worker:
    idle_sleep_time: int = 0.42
    worker_count: int = config.STREAM_WORKERS

    _worker_tasks: List = dataclasses.field(init=False, default_factory=list)
    _redis: Any = dataclasses.field(init=False, default=None)

    _loop: Any = dataclasses.field(init=False, default_factory=asyncio.get_running_loop)
    _stopping: Any = dataclasses.field(init=False, default_factory=asyncio.Event)
    _tombstone: Any = dataclasses.field(init=False, default_factory=asyncio.Event)

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
                                "worker %d release stream: %s", worker_id, stream_name,
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

            await self._sleep_or_stop(60)

    async def _run(self):
        self._stopping.clear()

        self._redis = await utils.create_aredis_for_stream()
        self._stream_selector = StreamSelector(self.worker_count, self._redis)

        for i in range(self.worker_count):
            self._worker_tasks.append(asyncio.create_task(self.stream_worker_task(i)))

        self._monitoring_task = asyncio.create_task(self.monitoring_task())

        LOG.debug("%d workers spawned", self.worker_count)

    async def _shutdown(self):
        LOG.debug("wait for workers to exit")
        self._stopping.set()

        await asyncio.wait(
            [self._start_task, self._monitoring_task] + self._worker_tasks
        )
        self._worker_tasks = []

        if self._redis:
            self._redis.connection_pool.disconnect()
            self._redis = None

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
    uvloop.install()
    logs.setup_logging(worker="streams")
    asyncio.run(run_forever())
