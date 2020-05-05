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

from datadog import statsd
import msgpack
import uvloop

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine.clients import github


LOG = logs.getLogger(__name__)


MAX_RETRIES = 3

WORKER_PROCESSING_DELAY = 30


@dataclasses.dataclass
class PullRetry(Exception):
    attempts: int


class MaxPullRetry(PullRetry):
    pass


@dataclasses.dataclass
class StreamRetry(Exception):
    attempts: int
    retry_at: datetime.datetime


def push(installation_id, owner, repo, pull_number, event_type, data):
    redis = utils.get_redis_for_stream()
    stream_name = f"stream~{installation_id}".encode()
    scheduled_at = utils.utcnow() + datetime.timedelta(seconds=WORKER_PROCESSING_DELAY)
    score = scheduled_at.timestamp()
    transaction = redis.pipeline()
    # NOTE(sileht): Add this event to the pull request stream
    transaction.xadd(
        stream_name,
        {
            "event": msgpack.packb(
                {
                    "owner": owner,
                    "repo": repo,
                    "pull_number": pull_number,
                    "source": {"event_type": event_type, "data": data},
                },
                use_bin_type=True,
            ),
        },
    )
    # NOTE(sileht): Add pull request stream to process to the list, only if it
    # does not exists, to not update the score(date)
    transaction.zadd("streams", {stream_name: score}, nx=True)
    transaction.execute()
    LOG.debug(
        "pushed to worker",
        gh_owner=owner,
        gh_repo=repo,
        gh_pull=pull_number,
        event_type=event_type,
    )


def run_engine(installation_id, owner, repo, pull_number, sources):
    logger = logs.getLogger(__name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number)
    logger.debug("engine in thread start")
    try:
        try:
            installation = github.get_installation(owner, repo, installation_id)
        except exceptions.MergifyNotInstalled:
            return
        logger.debug("engine get installation")
        with github.get_client(owner, repo, installation) as client:
            pull = client.item(f"pulls/{pull_number}")
            engine.run(client, pull, sources)
    finally:
        logger.debug("engine in thread end")


class EngineRunThread(threading.Thread):
    """This thread propagate exception to main thread."""

    def __init__(self):
        super().__init__(daemon=True)
        self._running = threading.Event()
        self._process = threading.Event()

        self._engine_args = None
        self._exception = None

        self.start()

    async def run_engine(self, *engine_args):
        self._engine_args = engine_args
        self._exception = None

        self._process.set()
        while self._process.is_set() and self._running.is_set():
            await asyncio.sleep(0.01)

        if not self._running.is_set():
            return

        if self._exception:
            raise self._exception

    def close(self):
        self._running.clear()
        self._process.set()
        self.join()

    def run(self):
        self._running.set()
        while self._running.is_set():
            self._process.wait()
            if not self._running.is_set():
                return

            try:
                run_engine(*self._engine_args)
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
    _thread: EngineRunThread = dataclasses.field(
        init=False, default_factory=EngineRunThread
    )

    def close(self):
        self._thread.close()

    async def _run_engine_and_translate_exception_to_retries(
        self, installation_id, owner, repo, pull_number, sources
    ):
        attempts_key = f"pull~{installation_id}~{owner}~{repo}~{pull_number}"
        try:
            await self._thread.run_engine(
                installation_id, owner, repo, pull_number, sources
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
            logger = logs.getLogger(
                __name__, gh_repo=repo, gh_owner=owner, gh_pull=pull_number
            )

            if isinstance(e, github.TooManyPages):
                # TODO(sileht): Ideally this should be catcher earlier to post an
                # appropriate check-runs to inform user the PR is too big to be handled
                # by Mergify, but this need a bit of refactory to do it, so in the
                # meantimes...
                logger.warning("too many pages", exc_info=True)
                await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", f"stream~{installation_id}")
                return

            if exceptions.should_be_ignored(e):
                logger.debug("ignored engine error", exc_info=True)
                await self.redis.hdel("attempts", attempts_key)
                await self.redis.hdel("attempts", f"stream~{installation_id}")
                return

            stream_name = f"stream~{installation_id}"

            backoff = exceptions.need_retry(e)
            if backoff is None:
                # NOTE(sileht): This is our fault, so retry until we fix the bug but
                # without increasing the attempts
                raise

            # TODO(sileht): In case of RateLimit no need to apply expo
            attempts = await self.redis.hincrby("attempts", stream_name)
            retry_in = 3 ** attempts * backoff
            retry_at = utils.utcnow() + datetime.timedelta(seconds=retry_in)
            score = retry_at.timestamp()
            await self.redis.zaddoption("streams", "XX", **{stream_name: score})
            raise StreamRetry(attempts, retry_at) from e

    async def consume(self, stream_name):
        installation_id = int(stream_name.split("~")[1])

        LOG.debug("read stream", stream_name=stream_name)
        messages = await self.redis.xrange(stream_name, count=100)
        # Groups stream by pull request
        pulls = collections.defaultdict(lambda: ([], []))
        statsd.histogram("engine.streams.size", len(messages))
        for message_id, message in messages:
            data = msgpack.unpackb(message[b"event"], raw=False)
            key = (data["owner"], data["repo"], data["pull_number"])
            pulls[key][0].append(message_id)
            pulls[key][1].append(data["source"])

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
                    installation_id, owner, repo, pull_number, sources
                )
                await self.redis.execute_command("XDEL", stream_name, *message_ids)
                end = time.monotonic()
                logger.debug("engine finished in %s sec", end - start)
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
            except StreamRetry as e:
                logger.info(
                    "failed to process stream, retrying",
                    attempts=e.attempts,
                    retry_at=e.retry_at,
                    exc_info=True,
                )
                return
            except Exception:
                logger.error("failed to process pull request", exc_info=True)

        LOG.debug("cleanup stream start", stream_name=stream_name)
        score = time.time()
        await self.redis.eval(
            self.ATOMIC_CLEAN_STREAM_SCRIPT, 1, stream_name.encode(), score
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


@dataclasses.dataclass
class Worker:
    idle_sleep_time: int = 0.42
    worker_count: int = config.STREAM_WORKERS

    _worker_tasks: List = dataclasses.field(init=False, default_factory=list)
    _redis: Any = dataclasses.field(init=False, default=None)

    _loop: Any = dataclasses.field(init=False, default_factory=asyncio.get_running_loop)
    _running: Any = dataclasses.field(init=False, default_factory=asyncio.Event)
    _tombstone: Any = dataclasses.field(init=False, default_factory=asyncio.Event)

    async def stream_worker_task(self, worker_id):
        # NOTE(sileht): This task must never fail, we don't want to write code to
        # reap/clean/respawn them
        stream_processor = StreamProcessor(self._redis)

        while self._running.is_set():
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
                        await asyncio.sleep(self.idle_sleep_time)
            except Exception:
                LOG.error("worker %d fail, sleeping a bit", worker_id, exc_info=True)
                await asyncio.sleep(self.idle_sleep_time)

        stream_processor.close()
        LOG.info("worker %d exited", worker_id)

    async def _run(self):
        self._running.set()

        self._redis = await utils.create_aredis_for_stream()
        self._stream_selector = StreamSelector(self.worker_count, self._redis)

        for i in range(self.worker_count):
            self._worker_tasks.append(asyncio.create_task(self.stream_worker_task(i)))

        LOG.info("%d workers spawned", self.worker_count)

    async def _shutdown(self):
        LOG.info("wait for workers to exit")
        self._running.clear()

        await asyncio.wait([self._start_task] + self._worker_tasks)
        self._worker_tasks = []

        if self._redis:
            self._redis.connection_pool.disconnect()
            self._redis = None

        self._tombstone.set()
        LOG.info("exiting")

    def start(self):
        self._start_task = asyncio.create_task(self._run())

    def stop(self):
        self._stop_task = asyncio.create_task(self._shutdown())

    async def wait_shutdown_complete(self):
        await self._tombstone.wait()
        await asyncio.wait([self._stop_task])

    def stop_with_signal(self, signame):
        if self._running.is_set():
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
