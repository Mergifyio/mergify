# -*- encoding: utf-8 -*-
# mypy: disallow-untyped-defs
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

#
# Current Redis layout:
#
#
#   +----------------+             +-----------------+                +-------------------+
#   |                |             |                 |                |                   |
#   |   stream       +-------------> Org 1           +----------------+  PR #123          |
#   |                +-            |                 +-               |                   |
#   +----------------+ \--         +-----------------+ \---           +-------------------+
#     Set of orgs         \--                              \--
#     to processs            \-    +-----------------+        \--     +-------------------+
#     key = org name           \-- |                 |           \--- |                   |
#     score = timestamp           \+ Org 2           |               \+ PR #456           |
#                                  |                 |                |                   |
#                                  +-----------------+                +-------------------+
#                                  Set of pull requests               Stream with appended
#                                  to process for each                GitHub events.
#                                  org                                PR #0 is for events with
#                                  key = pull request                 no PR number attached.
#                                  score = timestamp
#
#
# Orgs key format: f"bucket~{owner_id}~{owner_login}"
# Pull key format: f"bucket-sources~{repo_id}~{repo_name}~{pull_number or 0}"
#

import argparse
import asyncio
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
from ddtrace import tracer
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
from mergify_engine import service
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine import worker_lua
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

OrgBucketNameType = typing.NewType("OrgBucketNameType", str)


class IgnoredException(Exception):
    pass


@dataclasses.dataclass
class PullRetry(Exception):
    attempts: int


class MaxPullRetry(PullRetry):
    pass


@dataclasses.dataclass
class OrgBucketRetry(Exception):
    org_bucket_name: OrgBucketNameType
    attempts: int
    retry_at: datetime.datetime


class OrgBucketUnused(Exception):
    org_bucket_name: OrgBucketNameType


@dataclasses.dataclass
class UnexpectedPullRetry(Exception):
    pass


T_MessagePayload = typing.NewType("T_MessagePayload", typing.Dict[bytes, bytes])
# FIXME(sileht): redis returns bytes, not str
T_MessageID = typing.NewType("T_MessageID", str)


def get_low_priority_minimal_score() -> float:
    # NOTE(sileht): score is scheduled_at timestamp for high
    # prio and scheduled_at timestamp * 10 for low prio, pick *
    # 2 to split the bucket
    return date.utcnow().timestamp() * 2


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(aredis.ConnectionError),
    reraise=True,
)
async def push(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
    repo_id: github_types.GitHubRepositoryIdType,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: typing.Optional[github_types.GitHubPullRequestNumber],
    event_type: github_types.GitHubEventType,
    data: github_types.GitHubEvent,
    score: typing.Optional[str] = None,
) -> None:
    now = date.utcnow()
    event = msgpack.packb(
        {
            "event_type": event_type,
            "data": data,
            "timestamp": now.isoformat(),
        },
        use_bin_type=True,
    )
    scheduled_at = now + datetime.timedelta(seconds=WORKER_PROCESSING_DELAY)

    # NOTE(sileht): lower timestamps are processed first
    if score is None:
        score = str(date.utcnow().timestamp())

    await worker_lua.push_pull(
        redis,
        owner_id,
        owner_login,
        repo_id,
        repo_name,
        pull_number,
        scheduled_at,
        event,
        score,
    )
    LOG.debug(
        "pushed to worker",
        gh_owner=owner_login,
        gh_repo=repo_name,
        gh_pull=pull_number,
        event_type=event_type,
    )


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
            ctxt = await installation.get_pull_request_context(
                repo_id,
                pull_number,
                # NOTE(sileht): A pull request may be reevaluated during one call of
                # consume_buckets(), so we need to clear the sources/_cache/pull/... to
                # ensure we get the last snapshot of the pull request
                force_new=True,
            )
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


@dataclasses.dataclass
class OrgBucketSelector:
    redis_stream: utils.RedisStream
    worker_id: int
    worker_count: int

    def get_worker_id_for(self, org_bucket: bytes) -> int:
        return int(hashlib.md5(org_bucket).hexdigest(), 16) % self.worker_count  # nosec

    def _is_org_bucket_for_me(self, org_bucket: bytes) -> bool:
        return self.get_worker_id_for(org_bucket) == self.worker_id

    async def next_org_bucket(self) -> typing.Optional[OrgBucketNameType]:
        now = time.time()
        for org_bucket in await self.redis_stream.zrangebyscore(
            "streams",
            min=0,
            max=now,
        ):
            if self._is_org_bucket_for_me(org_bucket):
                statsd.increment(
                    "engine.streams.selected", tags=[f"worker_id:{self.worker_id}"]
                )
                return OrgBucketNameType(org_bucket.decode())

        return None


@dataclasses.dataclass
class StreamProcessor:
    redis_stream: utils.RedisStream
    redis_cache: utils.RedisCache

    @contextlib.asynccontextmanager
    async def _translate_exception_to_retries(
        self,
        org_bucket_name: OrgBucketNameType,
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
                await self.redis_stream.hdel("attempts", org_bucket_name)
                raise OrgBucketUnused(org_bucket_name)

            if isinstance(e, github.TooManyPages):
                # TODO(sileht): Ideally this should be catcher earlier to post an
                # appropriate check-runs to inform user the PR is too big to be handled
                # by Mergify, but this need a bit of refactory to do it, so in the
                # meantimes...
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", org_bucket_name)
                raise IgnoredException()

            if exceptions.should_be_ignored(e):
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", org_bucket_name)
                raise IgnoredException()

            if isinstance(e, exceptions.RateLimited):
                retry_at = date.utcnow() + e.countdown
                score = retry_at.timestamp()
                if attempts_key:
                    await self.redis_stream.hdel("attempts", attempts_key)
                await self.redis_stream.hdel("attempts", org_bucket_name)
                await self.redis_stream.zaddoption(
                    "streams", "XX", **{org_bucket_name: score}
                )
                raise OrgBucketRetry(org_bucket_name, 0, retry_at)

            backoff = exceptions.need_retry(e)
            if backoff is None:
                # NOTE(sileht): This is our fault, so retry until we fix the bug but
                # without increasing the attempts
                raise

            attempts = await self.redis_stream.hincrby("attempts", org_bucket_name)
            retry_in = 2 ** min(attempts, 3) * backoff
            retry_at = date.utcnow() + retry_in
            score = retry_at.timestamp()
            await self.redis_stream.zaddoption(
                "streams", "XX", **{org_bucket_name: score}
            )
            raise OrgBucketRetry(org_bucket_name, attempts, retry_at)

    async def consume(
        self,
        org_bucket_name: OrgBucketNameType,
        owner_id: github_types.GitHubAccountIdType,
        owner_login: github_types.GitHubLogin,
    ) -> None:
        LOG.debug("consoming org bucket", gh_owner=owner_login)

        try:
            async with self._translate_exception_to_retries(org_bucket_name):
                sub = await subscription.Subscription.get_subscription(
                    self.redis_cache, owner_id
                )
            async with github.aget_client(owner_id) as client:
                installation = context.Installation(
                    owner_id, owner_login, sub, client, self.redis_cache
                )

                async with self._translate_exception_to_retries(org_bucket_name):
                    await self._consume_buckets(org_bucket_name, installation)

                await self._refresh_merge_trains(org_bucket_name, installation)
        except aredis.exceptions.ConnectionError:
            statsd.increment("redis.client.connection.errors")
            LOG.warning(
                "Stream Processor lost Redis connection",
                org_bucket_name=org_bucket_name,
            )
        except OrgBucketUnused:
            LOG.info(
                "unused org bucket, dropping it", gh_owner=owner_login, exc_info=True
            )
            try:
                await worker_lua.drop_bucket(self.redis_stream, owner_id, owner_login)
            except aredis.exceptions.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning(
                    "fail to drop org bucket, it will be retried",
                    org_bucket_name=org_bucket_name,
                )
        except OrgBucketRetry as e:
            log_method = (
                LOG.error
                if e.attempts >= STREAM_ATTEMPTS_LOGGING_THRESHOLD
                else LOG.info
            )
            log_method(
                "failed to process org bucket, retrying",
                attempts=e.attempts,
                retry_at=e.retry_at,
                gh_owner=owner_login,
                exc_info=True,
            )
            return
        except vcr_errors_CannotOverwriteExistingCassetteException:
            LOG.error(
                "failed to process org bucket", gh_owner=owner_login, exc_info=True
            )
            # NOTE(sileht): During functionnal tests replay, we don't want to retry for ever
            # so we catch the error and print all events that can't be processed
            buckets = await self.redis_stream.zrangebyscore(
                org_bucket_name, min=0, max="+inf", start=0, num=1
            )
            for bucket in buckets:
                messages = await self.redis_stream.xrange(bucket)
                for _, message in messages:
                    LOG.info(msgpack.unpackb(message[b"source"], raw=False))
                await self.redis_stream.delete(bucket)
                await self.redis_stream.zrem(org_bucket_name, bucket)
        except Exception:
            # Ignore it, it will retried later
            LOG.error(
                "failed to process org bucket", gh_owner=owner_login, exc_info=True
            )

        LOG.debug("cleanup org bucket start", org_bucket_name=org_bucket_name)
        try:
            await worker_lua.clean_org_bucket(
                self.redis_stream,
                owner_id,
                owner_login,
                date.utcnow(),
            )
        except aredis.exceptions.ConnectionError:
            statsd.increment("redis.client.connection.errors")
            LOG.warning(
                "fail to cleanup org bucket, it maybe partially replayed",
                org_bucket_name=org_bucket_name,
            )
        LOG.debug("cleanup org bucket end", org_bucket_name=org_bucket_name)

    async def _refresh_merge_trains(
        self, org_bucket_name: OrgBucketNameType, installation: context.Installation
    ) -> None:
        async with self._translate_exception_to_retries(
            org_bucket_name,
        ):
            async for train in merge_train.Train.iter_trains(installation):
                await train.load()
                await train.refresh()

    @staticmethod
    def _extract_infos_from_bucket_sources_key(
        bucket_sources_key: bytes,
    ) -> typing.Tuple[
        github_types.GitHubRepositoryIdType,
        github_types.GitHubRepositoryName,
        github_types.GitHubPullRequestNumber,
    ]:
        _, repo_id, repo_name, pull_number = bucket_sources_key.split(b"~")
        return (
            github_types.GitHubRepositoryIdType(int(repo_id)),
            github_types.GitHubRepositoryName(repo_name.decode()),
            github_types.GitHubPullRequestNumber(int(pull_number)),
        )

    async def _consume_buckets(
        self, bucket_key: OrgBucketNameType, installation: context.Installation
    ) -> None:
        opened_pulls_by_repo: typing.Dict[
            github_types.GitHubRepositoryName,
            typing.List[github_types.GitHubPullRequest],
        ] = {}

        need_retries_later = set()

        pulls_processed = 0
        started_at = time.monotonic()
        while True:
            bucket_sources_keys = await self.redis_stream.zrangebyscore(
                bucket_key,
                min=0,
                max="+inf",
                withscores=True,
            )
            LOG.debug(
                "org bucket contains %d pulls",
                len(bucket_sources_keys),
                gh_owner=installation.owner_login,
            )
            for bucket_sources_key, _bucket_score in bucket_sources_keys:
                (
                    repo_id,
                    repo_name,
                    pull_number,
                ) = self._extract_infos_from_bucket_sources_key(bucket_sources_key)
                if (repo_id, repo_name, pull_number) in need_retries_later:
                    continue
                break
            else:
                break

            if (time.monotonic() - started_at) >= config.BUCKET_PROCESSING_MAX_SECONDS:
                low_prio_threshold = get_low_priority_minimal_score()
                prio = "high" if _bucket_score < low_prio_threshold else "low"
                statsd.increment("engine.buckets.preempted", tags=[f"priority:{prio}"])
                break

            pulls_processed += 1
            installation.client.set_requests_ratio(pulls_processed)

            logger = daiquiri.getLogger(
                __name__,
                gh_owner=installation.owner_login,
                gh_repo=repo_name,
                gh_pull=pull_number,
            )

            messages = await self.redis_stream.xrange(bucket_sources_key)
            statsd.histogram("engine.buckets.events.read_size", len(messages))  # type: ignore[no-untyped-call]
            logger.debug("read org bucket", sources=len(messages))
            if not messages:
                # Should not occur but better be safe than sorry
                await worker_lua.remove_pull(
                    self.redis_stream,
                    installation.owner_id,
                    installation.owner_login,
                    repo_id,
                    repo_name,
                    pull_number,
                    (),
                )
                break

            if bucket_sources_key.endswith(b"~0"):
                logger.debug(
                    "unpack events without pull request number", count=len(messages)
                )
                if repo_name not in opened_pulls_by_repo:
                    try:
                        opened_pulls_by_repo[repo_name] = [
                            p
                            async for p in installation.client.items(
                                f"/repos/{installation.owner_login}/{repo_name}/pulls",
                            )
                        ]
                    except Exception as e:
                        if exceptions.should_be_ignored(e):
                            opened_pulls_by_repo[repo_name] = []
                        else:
                            raise

                for message_id, message in messages:
                    source = typing.cast(
                        context.T_PayloadEventSource,
                        msgpack.unpackb(message[b"source"], raw=False),
                    )
                    converted_messages = await self._convert_event_to_messages(
                        installation,
                        repo_id,
                        repo_name,
                        source,
                        opened_pulls_by_repo[repo_name],
                        message[b"score"],
                    )
                    logger.debug("event unpacked into %d messages", converted_messages)
                    # NOTE(sileht) can we take the risk to batch the deletion here ?
                    await worker_lua.remove_pull(
                        self.redis_stream,
                        installation.owner_id,
                        installation.owner_login,
                        repo_id,
                        repo_name,
                        pull_number,
                        (typing.cast(T_MessageID, message_id),),
                    )
            else:
                sources = [
                    typing.cast(
                        context.T_PayloadEventSource,
                        msgpack.unpackb(message[b"source"], raw=False),
                    )
                    for _, message in messages
                ]
                message_ids = [
                    typing.cast(T_MessageID, message_id) for message_id, _ in messages
                ]
                logger.debug(
                    "consume pull request",
                    count=len(messages),
                    sources=sources,
                    message_ids=message_ids,
                )
                try:
                    with tracer.trace(
                        "pull processing",
                        span_type="worker",
                        resource=f"{installation.owner_login}/{repo_name}/{pull_number}",
                    ) as span:
                        span.set_tags({"gh_repo": repo_name, "gh_pull": pull_number})
                        await self._consume_pull(
                            bucket_key,
                            installation,
                            repo_id,
                            repo_name,
                            pull_number,
                            message_ids,
                            sources,
                        )
                except OrgBucketRetry:
                    raise
                except OrgBucketUnused:
                    raise
                except vcr_errors_CannotOverwriteExistingCassetteException:
                    raise
                except (PullRetry, UnexpectedPullRetry):
                    need_retries_later.add((repo_id, repo_name, pull_number))

        statsd.histogram("engine.buckets.read_size", pulls_processed)  # type: ignore[no-untyped-call]

    async def _convert_event_to_messages(
        self,
        installation: context.Installation,
        repo_id: github_types.GitHubRepositoryIdType,
        repo_name: github_types.GitHubRepositoryName,
        source: context.T_PayloadEventSource,
        pulls: typing.List[github_types.GitHubPullRequest],
        score: typing.Optional[str] = None,
    ) -> int:
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

        # NOTE(sileht): refreshing all opened pull request because something got merged
        # has a lower priority
        if source["event_type"] == "push":
            score = str(date.utcnow().timestamp() * 10)

        for pull_number in pull_numbers:
            if pull_number is None:
                # NOTE(sileht): even it looks not possible, this is a safeguard to ensure
                # we didn't generate a ending loop of events, because when pull_number is
                # None, this method got called again and again.
                raise RuntimeError("Got an empty pull number")
            await push(
                self.redis_stream,
                installation.owner_id,
                installation.owner_login,
                repo_id,
                repo_name,
                pull_number,
                source["event_type"],
                source["data"],
                score,
            )
        return len(pull_numbers)

    async def _consume_pull(
        self,
        org_bucket_name: OrgBucketNameType,
        installation: context.Installation,
        repo_id: github_types.GitHubRepositoryIdType,
        repo_name: github_types.GitHubRepositoryName,
        pull_number: github_types.GitHubPullRequestNumber,
        message_ids: typing.List[T_MessageID],
        sources: typing.List[context.T_PayloadEventSource],
    ) -> None:
        for source in sources:
            if "timestamp" in source:
                if source["event_type"] == "push":
                    metric = "engine.buckets.push-events.latency"
                else:
                    metric = "engine.buckets.events.latency"

                statsd.histogram(  # type: ignore[no-untyped-call]
                    metric,
                    (
                        date.utcnow() - date.fromisoformat(source["timestamp"])
                    ).total_seconds(),
                )

        logger = daiquiri.getLogger(
            __name__,
            gh_repo=repo_name,
            gh_owner=installation.owner_login,
            gh_pull=pull_number,
        )

        attempts_key = f"pull~{installation.owner_login}~{repo_name}~{pull_number}"
        try:
            async with self._translate_exception_to_retries(
                org_bucket_name,
                attempts_key,
            ):
                await run_engine(installation, repo_id, repo_name, pull_number, sources)
            await self.redis_stream.hdel("attempts", attempts_key)
            await worker_lua.remove_pull(
                self.redis_stream,
                installation.owner_id,
                installation.owner_login,
                repo_id,
                repo_name,
                pull_number,
                tuple(message_ids),
            )
        except IgnoredException:
            await worker_lua.remove_pull(
                self.redis_stream,
                installation.owner_id,
                installation.owner_login,
                repo_id,
                repo_name,
                pull_number,
                tuple(message_ids),
            )
            logger.debug("failed to process pull request, ignoring", exc_info=True)
        except MaxPullRetry as e:
            await worker_lua.remove_pull(
                self.redis_stream,
                installation.owner_id,
                installation.owner_login,
                repo_id,
                repo_name,
                pull_number,
                tuple(message_ids),
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
            raise
        except OrgBucketRetry:
            raise
        except OrgBucketUnused:
            raise
        except vcr_errors_CannotOverwriteExistingCassetteException:
            raise
        except Exception:
            # Ignore it, it will retried later
            logger.error("failed to process pull request", exc_info=True)
            raise UnexpectedPullRetry()


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
    def worker_count(self) -> int:
        return self.worker_per_process * self.process_count

    @staticmethod
    def _extract_owner(
        org_bucket_name: OrgBucketNameType,
    ) -> typing.Tuple[github_types.GitHubAccountIdType, github_types.GitHubLogin]:
        org_bucket_splitted = org_bucket_name.split("~")[1:]
        return (
            github_types.GitHubAccountIdType(int(org_bucket_splitted[0])),
            github_types.GitHubLogin(org_bucket_splitted[1]),
        )

    async def stream_worker_task(self, worker_id: int) -> None:
        if self._redis_stream is None or self._redis_cache is None:
            raise RuntimeError("redis clients are not ready")

        log_context_token = logs.WORKER_ID.set(worker_id)

        # NOTE(sileht): This task must never fail, we don't want to write code to
        # reap/clean/respawn them
        stream_processor = StreamProcessor(self._redis_stream, self._redis_cache)
        org_bucket_selector = OrgBucketSelector(
            self._redis_stream, worker_id, self.worker_count
        )

        while not self._stopping.is_set():
            try:
                org_bucket_name = await org_bucket_selector.next_org_bucket()
                if org_bucket_name:
                    LOG.debug(
                        "worker %s take org bucket: %s", worker_id, org_bucket_name
                    )
                    owner_id, owner_login = self._extract_owner(org_bucket_name)
                    try:
                        with tracer.trace(
                            "org bucket processing",
                            span_type="worker",
                            resource=owner_login,
                        ) as span:
                            span.set_tag("gh_owner", owner_login)
                            with statsd.timed("engine.stream.consume.time"):  # type: ignore[no-untyped-call]
                                await stream_processor.consume(
                                    org_bucket_name, owner_id, owner_login
                                )
                    finally:
                        LOG.debug(
                            "worker %s release org bucket: %s",
                            worker_id,
                            org_bucket_name,
                        )
                else:
                    LOG.debug("worker %s has nothing to do, sleeping a bit", worker_id)
                    await self._sleep_or_stop()
            except asyncio.CancelledError:
                LOG.debug("worker %s killed", worker_id)
                return
            except aredis.exceptions.ConnectionError:
                statsd.increment("redis.client.connection.errors")
                LOG.warning("worker %s lost Redis connection", worker_id, exc_info=True)
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
                # TODO(sileht): maybe also graph streams that are before `now`
                # to see the diff between the backlog and the upcoming work to do
                now = time.time()
                org_buckets = await self._redis_stream.zrangebyscore(
                    "streams",
                    min=0,
                    max=now,
                    withscores=True,
                )
                # NOTE(sileht): The latency may not be exact with the next StreamSelector
                # based on hash+modulo
                if len(org_buckets) > self.worker_count:
                    latency = now - org_buckets[self.worker_count][1]
                    statsd.timing("engine.streams.latency", latency)  # type: ignore[no-untyped-call]
                else:
                    statsd.timing("engine.streams.latency", 0)  # type: ignore[no-untyped-call]

                statsd.gauge("engine.streams.backlog", len(org_buckets))
                statsd.gauge("engine.workers.count", self.worker_count)
                statsd.gauge("engine.processes.count", self.process_count)
                statsd.gauge(
                    "engine.workers-per-process.count", self.worker_per_process
                )

                # TODO(sileht): maybe we can do something with the bucket scores to
                # build a latency metric
                bucket_backlog_low = 0
                bucket_backlog_high = 0
                for org_bucket, _ in org_buckets:
                    bucket_contents = await self._redis_stream.zrangebyscore(
                        org_bucket, min=0, max="+inf", withscores=True
                    )
                    low_prio_threshold = get_low_priority_minimal_score()
                    for _, score in bucket_contents:
                        if score < low_prio_threshold:
                            bucket_backlog_high += 1
                        else:
                            bucket_backlog_low += 1

                statsd.gauge(
                    "engine.buckets.backlog",
                    bucket_backlog_high,
                    tags=["priority:high"],
                )
                statsd.gauge(
                    "engine.buckets.backlog", bucket_backlog_low, tags=["priority:low"]
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
    service.setup("worker")
    signals.setup()
    return asyncio.run(run_forever())


async def async_status() -> None:
    worker_per_process: int = config.STREAM_WORKERS_PER_PROCESS
    process_count: int = config.STREAM_PROCESSES
    worker_count: int = worker_per_process * process_count

    redis_stream = utils.create_aredis_for_stream()
    org_bucket_selector = OrgBucketSelector(redis_stream, 0, worker_count)

    def sorter(item: typing.Tuple[bytes, float]) -> int:
        org_bucket, score = item
        return org_bucket_selector.get_worker_id_for(org_bucket)

    org_buckets = sorted(
        await redis_stream.zrangebyscore("streams", min=0, max="+inf", withscores=True),
        key=sorter,
    )

    for worker_id, org_buckets_by_worker in itertools.groupby(org_buckets, key=sorter):
        for org_bucket, score in org_buckets_by_worker:
            date = datetime.datetime.utcfromtimestamp(score).isoformat(" ", "seconds")
            owner = org_bucket.split(b"~")[2]
            event_org_buckets = await redis_stream.zrange(org_bucket, 0, -1)
            count = sum([await redis_stream.xlen(es) for es in event_org_buckets])
            items = f"{len(event_org_buckets)} pull requests, {count} events"
            print(f"{{{worker_id:02}}} [{date}] {owner.decode()}: {items}")


def status() -> None:
    asyncio.run(async_status())


async def async_reschedule_now() -> int:
    parser = argparse.ArgumentParser(description="Rescheduler for Mergify")
    parser.add_argument("org", help="Organization")
    args = parser.parse_args()

    redis = utils.create_aredis_for_stream()
    org_buckets = await redis.zrangebyscore("streams", min=0, max="+inf")
    expected_org = f"~{args.org.lower()}"
    for org_bucket in org_buckets:
        if org_bucket.decode().lower().endswith(expected_org):
            scheduled_at = date.utcnow()
            score = scheduled_at.timestamp()
            transaction = await redis.pipeline()
            await transaction.hdel("attempts", org_bucket)
            # TODO(sileht): Should we update bucket scores too ?
            await transaction.zadd("streams", **{org_bucket.decode(): score})
            # NOTE(sileht): Do we need to cleanup the per PR attempt?
            # await transaction.hdel("attempts", attempts_key)
            await transaction.execute()
            return 0
    else:
        print(f"Stream for {args.org} not found")
        return 1


def reschedule_now() -> int:
    return asyncio.run(async_reschedule_now())
