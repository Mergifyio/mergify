# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
import dataclasses
import datetime
import time
import typing

import daiquiri
import tenacity

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import logs
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

HOUR = datetime.timedelta(hours=1).total_seconds()


# We use dataclasses with a special hash method to merge same org/repo id even
# when their got renamed
@dataclasses.dataclass(unsafe_hash=True, order=True)
class SeatAccount:
    id: github_types.GitHubAccountIdType
    login: github_types.GitHubLogin = dataclasses.field(compare=False)


class ActiveUser(SeatAccount):
    pass


@dataclasses.dataclass(unsafe_hash=True, order=True)
class SeatRepository:
    id: github_types.GitHubRepositoryIdType
    name: github_types.GitHubRepositoryName = dataclasses.field(compare=False)


class SeatsCountResultT(typing.NamedTuple):
    write: int
    active: int


class CollaboratorsSetsT(typing.TypedDict):
    write: typing.Set[SeatAccount]
    active: typing.Set[ActiveUser]


CollaboratorsT = typing.Dict[
    SeatAccount,
    typing.Dict[SeatRepository, CollaboratorsSetsT],
]

ACTIVE_USERS_PREFIX = "active-users"


ActiveUserKeyT = typing.NewType("ActiveUserKeyT", str)


def get_active_users_key(repository: github_types.GitHubRepository) -> ActiveUserKeyT:
    return ActiveUserKeyT(
        f"{ACTIVE_USERS_PREFIX}~{repository['owner']['id']}~{repository['owner']['login']}~{repository['id']}~{repository['name']}"
    )


async def get_active_users_keys(
    redis_cache: utils.RedisCache,
    owner_id: typing.Union[typing.Literal["*"], github_types.GitHubAccountIdType] = "*",
    repo_id: typing.Union[
        typing.Literal["*"], github_types.GitHubRepositoryIdType
    ] = "*",
) -> typing.AsyncIterator[ActiveUserKeyT]:
    async for key in redis_cache.scan_iter(
        f"{ACTIVE_USERS_PREFIX}~{owner_id}~*~{repo_id}~*", count=10000
    ):
        yield ActiveUserKeyT(key)


def _parse_user(user: str) -> ActiveUser:
    part1, part2 = user.split("~")
    return ActiveUser(
        github_types.GitHubAccountIdType(int(part1)), github_types.GitHubLogin(part2)
    )


async def get_active_users(
    redis_cache: utils.RedisCache, key: ActiveUserKeyT
) -> typing.Set[ActiveUser]:
    one_month_ago = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    return {
        _parse_user(user)
        for user in await redis_cache.zrangebyscore(
            key, min=one_month_ago.timestamp(), max="+inf"
        )
    }


async def store_active_users(
    redis_cache: utils.RedisCache, event_type: str, event: github_types.GitHubEvent
) -> None:
    typed_event: typing.Optional[
        typing.Union[
            github_types.GitHubEventPush,
            github_types.GitHubEventIssueComment,
            github_types.GitHubEventPullRequest,
            github_types.GitHubEventPullRequestReview,
            github_types.GitHubEventPullRequestReviewComment,
        ]
    ] = None

    if event_type == "push":
        typed_event = typing.cast(github_types.GitHubEventPush, event)
    elif event_type == "issue_comment":
        typed_event = typing.cast(github_types.GitHubEventIssueComment, event)
    elif event_type == "pull_request":
        typed_event = typing.cast(github_types.GitHubEventPullRequest, event)
    elif event_type == "pull_request_review":
        typed_event = typing.cast(github_types.GitHubEventPullRequestReview, event)
    elif event_type == "pull_request_review_comment":
        typed_event = typing.cast(
            github_types.GitHubEventPullRequestReviewComment, event
        )

    if typed_event is not None:
        # TODO(sileht): sender is generic but when bot inpersonnate an used
        # if can differ from the real user, for example we may need to also
        # count for issue_comment the comment.user.id
        user = f"{typed_event['sender']['id']}~{typed_event['sender']['login']}"

        key = get_active_users_key(typed_event["repository"])
        await redis_cache.zadd(key, **{user: time.time()})


def count_seats(collaborators: CollaboratorsT) -> SeatsCountResultT:
    all_write_collaborators = set()
    all_active_collaborators = set()
    for repos in collaborators.values():
        for sets in repos.values():
            all_write_collaborators |= sets["write"]
            all_active_collaborators |= sets["active"]
    return SeatsCountResultT(
        len(all_write_collaborators), len(all_active_collaborators)
    )


async def get_collaborators() -> CollaboratorsT:
    all_collaborators: CollaboratorsT = collections.defaultdict(
        lambda: collections.defaultdict(lambda: {"write": set(), "active": set()})
    )
    redis_cache = utils.create_aredis_for_cache()

    async with github.AsyncGithubClient(
        auth=github_app.GithubBearerAuth(),
    ) as app_client:
        async for installation in app_client.items("/app/installations"):
            installation = typing.cast(github_types.GitHubInstallation, installation)
            org = SeatAccount(
                installation["account"]["id"], installation["account"]["login"]
            )
            async with github.aget_client(org.login, org.id) as client:
                async for repository in client.items(
                    "/installation/repositories", list_items="repositories"
                ):
                    repository = typing.cast(github_types.GitHubRepository, repository)
                    repo = SeatRepository(repository["id"], repository["name"])
                    async for collaborator in client.items(
                        f"{repository['url']}/collaborators"
                    ):
                        if collaborator["permissions"]["push"]:
                            all_collaborators[org][repo]["write"].add(
                                SeatAccount(collaborator["id"], collaborator["login"])
                            )

    async for key in get_active_users_keys(redis_cache):
        _, owner_id, owner_login, repo_id, repo_name = key.split("~")
        org = SeatAccount(
            github_types.GitHubAccountIdType(int(owner_id)),
            github_types.GitHubLogin(owner_login),
        )
        repo = SeatRepository(
            github_types.GitHubRepositoryIdType(int(repo_id)),
            github_types.GitHubRepositoryName(repo_name),
        )
        all_collaborators[org][repo]["active"] |= await get_active_users(
            redis_cache, key
        )

    return all_collaborators


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    reraise=True,
)
async def send_seats(seats: SeatsCountResultT) -> None:
    async with http.AsyncClient() as client:
        try:
            await client.post(
                f"{config.SUBSCRIPTION_BASE_URL}/on-premise/report",
                headers={"Authorization": f"token {config.SUBSCRIPTION_TOKEN}"},
                json={
                    "seats": seats.write,
                    "seats_write": seats.write,
                    "seats_active": seats.active,
                },
            )
        except Exception as exc:
            if exceptions.should_be_ignored(exc):
                return
            elif exceptions.need_retry(exc):
                raise tenacity.TryAgain
            else:
                raise


async def count_and_send() -> None:
    await asyncio.sleep(HOUR)
    while True:
        # NOTE(sileht): We loop even if SUBSCRIPTION_TOKEN is missing to not
        # break `tox -e test`. And we can et SUBSCRIPTION_TOKEN to test the
        # daemon with `tox -etest`
        if config.SUBSCRIPTION_TOKEN is None:
            LOG.info("on-premise subscription token missing, nothing to do.")
        else:
            try:
                seats = count_seats(await get_collaborators())
            except Exception:
                LOG.error("failed to count seats", exc_info=True)
            else:
                try:
                    await send_seats(seats)
                except Exception:
                    LOG.error("failed to send seats usage", exc_info=True)
            LOG.info("reported seats usage", seats=seats)

        await asyncio.sleep(12 * HOUR)


def to_json(collaborators: CollaboratorsT) -> str:
    data: typing.Dict[
        str, typing.Dict[str, typing.Dict[str, typing.Set[str]]]
    ] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: {"write": set(), "active": set()})
    )
    for org, repos in collaborators.items():
        for repo, _seats in repos.items():
            data[org.login][repo.name] = {
                "write": {seat.login for seat in _seats["write"]},
                "active": {seat.login for seat in _seats["active"]},
            }
    return json.dumps(data)


def report(args: argparse.Namespace) -> None:
    if args.daemon:
        logs.setup_logging()
        asyncio.run(count_and_send())
    else:
        logs.setup_logging(dump_config=False)
        if config.SUBSCRIPTION_TOKEN is None:
            LOG.error("on-premise subscription token missing")
        else:
            collaborators = asyncio.run(get_collaborators())
            if args.json:
                print(to_json(collaborators))
            else:
                seats = count_seats(collaborators)
                LOG.info("collaborators with write access: %s", seats.write)
                LOG.info("active collaborators: %s", seats.active)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report used seats")
    parser.add_argument(
        "--daemon",
        "-d",
        action="store_true",
        help="Run as daemon and report usage regularly",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Output detailed usage in JSON format",
    )
    return report(parser.parse_args())
