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
import hashlib
import hmac
import math
import os
import socket
import typing

import daiquiri

from mergify_engine import date
from mergify_engine import github_types


if typing.TYPE_CHECKING:
    from mergify_engine import redis_utils


LOG = daiquiri.getLogger()

_PROCESS_IDENTIFIER = os.environ.get("DYNO") or socket.gethostname()


def unicode_truncate(
    s: str,
    length: int,
    placeholder: str = "",
    encoding: str = "utf-8",
) -> str:
    """Truncate a string to length in bytes.

    :param s: The string to truncate.
    :param length: The length in number of bytes — not characters (placeholder included).
    :param placeholder: String that will appear at the end of the output text if it has been truncated.
    """
    b = s.encode(encoding)
    if len(b) > length:
        placeholder_bytes = placeholder.encode(encoding)
        placeholder_length = len(placeholder_bytes)
        if placeholder_length > length:
            raise ValueError(
                "`placeholder` length must be greater or equal to `length`"
            )

        cut_at = length - placeholder_length

        return (b[:cut_at] + placeholder_bytes).decode(encoding, errors="ignore")
    else:
        return s


def compute_hmac(data: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode("utf8"), msg=data, digestmod=hashlib.sha1)
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
    redis_stream: "redis_utils.RedisStream",
    repository: github_types.GitHubRepository,
    action: github_types.GitHubEventRefreshActionType,
    source: str,
    pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber] = None,
    ref: typing.Optional[github_types.GitHubRefType] = None,
    score: typing.Optional[str] = None,
) -> None:
    # TODO(sileht): move refresh stuff into it's own file
    # Break circular import
    from mergify_engine import github_events
    from mergify_engine import worker

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

    slim_event = github_events._extract_slim_event("refresh", data)
    await worker.push(
        redis_stream,
        repository["owner"]["id"],
        repository["owner"]["login"],
        repository["id"],
        repository["name"],
        pull_request_number,
        "refresh",
        slim_event,
        None,
    )


async def send_pull_refresh(
    redis_stream: "redis_utils.RedisStream",
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
        redis_stream,
        repository,
        action,
        source,
        pull_request_number=pull_request_number,
    )


async def send_repository_refresh(
    redis_stream: "redis_utils.RedisStream",
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
    await _send_refresh(redis_stream, repository, action, source, score=score)


async def send_branch_refresh(
    redis_stream: "redis_utils.RedisStream",
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
    await _send_refresh(redis_stream, repository, action, source, ref=ref, score=score)


_T = typing.TypeVar("_T")


def split_list(
    remaining: typing.List[_T], part: int
) -> typing.Generator[typing.List[_T], None, None]:
    size = math.ceil(len(remaining) / part)
    while remaining:
        yield remaining[:size]
        remaining = remaining[size:]
