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
import contextlib
import hashlib
import hmac
import math
import os
import socket
import ssl
import typing
import uuid

import daiquiri
import yaaredis

from mergify_engine import config
from mergify_engine import date
from mergify_engine import github_types


LOG = daiquiri.getLogger()

_PROCESS_IDENTIFIER = os.environ.get("DYNO") or socket.gethostname()

# NOTE(sileht): I wonder with mypy thing yaaredis.StrictRedis is Any...
RedisCache = typing.NewType("RedisCache", yaaredis.StrictRedis)  # type: ignore
RedisStream = typing.NewType("RedisStream", yaaredis.StrictRedis)  # type: ignore
RedisQueue = typing.NewType("RedisQueue", yaaredis.StrictRedis)  # type: ignore


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


def create_yaaredis_for_cache(
    max_idle_time: int = 60, max_connections: typing.Optional[int] = None
) -> RedisCache:
    client = redis_from_url(
        config.STORAGE_URL,
        decode_responses=True,
        max_idle_time=max_idle_time,
        max_connections=max_connections,
    )
    return RedisCache(client)


@contextlib.contextmanager
def yaaredis_for_cache() -> typing.Iterator[RedisCache]:
    client = create_yaaredis_for_cache(max_idle_time=0)
    try:
        yield client
    finally:
        client.connection_pool.disconnect()


def create_yaaredis_for_stream(
    max_idle_time: int = 60,
    max_connections: typing.Optional[int] = None,
) -> RedisStream:
    r = redis_from_url(
        config.STREAM_URL, max_idle_time=max_idle_time, max_connections=max_connections
    )
    return RedisStream(r)


@contextlib.contextmanager
def yaaredis_for_stream() -> typing.Iterator[RedisCache]:
    client = create_yaaredis_for_stream(max_idle_time=0)
    try:
        yield client
    finally:
        client.connection_pool.disconnect()


def create_yaaredis_for_queue(
    max_idle_time: int = 60,
    max_connections: typing.Optional[int] = None,
) -> RedisQueue:
    r = redis_from_url(
        config.QUEUE_URL, max_idle_time=max_idle_time, max_connections=max_connections
    )
    return RedisQueue(r)


@contextlib.contextmanager
def yaaredis_for_queue() -> typing.Iterator[RedisQueue]:
    client = create_yaaredis_for_queue(max_idle_time=0)
    try:
        yield client
    finally:
        client.connection_pool.disconnect()


async def stop_pending_yaaredis_tasks() -> None:
    tasks = [
        task
        for task in asyncio.all_tasks()
        if (
            task.get_coro().__qualname__  # type: ignore[union-attr]
            == "ConnectionPool.disconnect_on_idle_time_exceeded"
        )
    ]

    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)


def unicode_truncate(s: str, length: int, encoding: str = "utf-8") -> str:
    """Truncate a string to length in bytes.

    :param s: The string to truncate.
    :param length: The length in number of bytes — not characters."""
    return s.encode(encoding)[:length].decode(encoding, errors="ignore")


def compute_hmac(data: bytes) -> str:
    mac = hmac.new(
        config.WEBHOOK_SECRET.encode("utf8"), msg=data, digestmod=hashlib.sha1
    )
    return str(mac.hexdigest())


class SupportsLessThan(typing.Protocol):
    def __lt__(self, __other: typing.Any) -> bool:
        ...


SupportsLessThanT = typing.TypeVar("SupportsLessThanT", bound=SupportsLessThan)


def get_random_choices(
    random_number: int, population: typing.Dict[SupportsLessThanT, int], k: int = 1
) -> typing.Set[SupportsLessThanT]:
    """Return a random number of item from a population without replacement.

    You need to provide the random number yourself.

    The output is always the same based on that number.

    The population is a dict where the key is the choice and the value is the weight.

    The argument k is the number of item that should be picked.

    :param random_number: The random_number that should be picked.
    :param population: The dict of {item: weight}.
    :param k: The number of choices to make.
    :return: A set with the choices.
    """
    if k > len(population):
        raise ValueError("k cannot be greater than the population size")

    picked: typing.Set[SupportsLessThanT] = set()
    population = population.copy()

    while len(picked) < k:
        total_weight = sum(population.values())
        choice_index = (random_number % total_weight) + 1
        for item in sorted(population.keys()):
            choice_index -= population[item]
            if choice_index <= 0:
                picked.add(item)
                del population[item]
                break

    return picked


ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def to_ordinal_numeric(number: int) -> str:
    if number < 0:
        raise ValueError("number must be positive")
    last = number % 100
    if last in (11, 12, 13):
        suffix = "th"
    else:
        last = number % 10
        suffix = ORDINAL_SUFFIXES.get(last) or "th"
    return f"{number}{suffix}"


class FakePR:
    def __init__(self, key: str, value: typing.Any):
        setattr(self, key, value)


async def _send_refresh(
    redis_cache: RedisCache,
    redis_stream: RedisStream,
    repository: github_types.GitHubRepository,
    action: github_types.GitHubEventRefreshActionType,
    source: str,
    pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber] = None,
    ref: typing.Optional[github_types.GitHubRefType] = None,
    score: typing.Optional[str] = None,
) -> None:
    # Break circular import
    from mergify_engine import github_events

    data = github_types.GitHubEventRefresh(
        {
            "action": action,
            "source": source,
            "ref": ref,
            "pull_request_number": pull_request_number,
            "repository": repository,
            "sender": {
                "login": github_types.GitHubLogin("<internal>"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
                "avatar_url": "",
            },
            "organization": repository["owner"],
            "installation": {
                "id": github_types.GitHubInstallationIdType(0),
                "account": repository["owner"],
                "target_type": repository["owner"]["type"],
                "permissions": {},
            },
        }
    )

    await github_events.filter_and_dispatch(
        redis_cache, redis_stream, "refresh", str(uuid.uuid4()), data
    )


async def send_pull_refresh(
    redis_cache: RedisCache,
    redis_stream: RedisStream,
    repository: github_types.GitHubRepository,
    action: github_types.GitHubEventRefreshActionType,
    pull_request_number: github_types.GitHubPullRequestNumber,
    source: str,
) -> None:
    LOG.info(
        "sending pull refresh",
        gh_owner=repository["owner"]["login"],
        gh_repo=repository["name"],
        gh_private=repository["private"],
        gh_pull=pull_request_number,
        action=action,
        source=source,
    )

    await _send_refresh(
        redis_cache,
        redis_stream,
        repository,
        action,
        source,
        pull_request_number=pull_request_number,
    )


async def send_repository_refresh(
    redis_cache: RedisCache,
    redis_stream: RedisStream,
    repository: github_types.GitHubRepository,
    action: github_types.GitHubEventRefreshActionType,
    source: str,
) -> None:

    LOG.info(
        "sending repository refresh",
        gh_owner=repository["owner"]["login"],
        gh_repo=repository["name"],
        gh_private=repository["private"],
        action=action,
        source=source,
    )

    score = str(date.utcnow().timestamp() * 10)
    await _send_refresh(
        redis_cache, redis_stream, repository, action, source, score=score
    )


async def send_branch_refresh(
    redis_cache: RedisCache,
    redis_stream: RedisStream,
    repository: github_types.GitHubRepository,
    action: github_types.GitHubEventRefreshActionType,
    ref: github_types.GitHubRefType,
    source: str,
) -> None:
    LOG.info(
        "sending repository branch refresh",
        gh_owner=repository["owner"]["login"],
        gh_repo=repository["name"],
        gh_private=repository["private"],
        gh_ref=ref,
        action=action,
        source=source,
    )
    score = str(date.utcnow().timestamp() * 10)
    await _send_refresh(
        redis_cache, redis_stream, repository, action, source, ref=ref, score=score
    )


_T = typing.TypeVar("_T")


def split_list(
    remaining: typing.List[_T], part: int
) -> typing.Generator[typing.List[_T], None, None]:
    size = math.ceil(len(remaining) / part)
    while remaining:
        yield remaining[:size]
        remaining = remaining[size:]
