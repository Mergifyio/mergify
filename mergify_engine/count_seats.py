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
from mergify_engine import service
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

HOUR = datetime.timedelta(hours=1).total_seconds()
ACTIVE_USERS_PREFIX = "active-users"


ActiveUserKeyT = typing.NewType("ActiveUserKeyT", str)


# We use dataclasses with a special hash method to merge same org/repo id even
# when they are renamed
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
    write_users: int
    active_users: int


class CollaboratorsSetsT(typing.TypedDict):
    write_users: typing.Optional[typing.Set[SeatAccount]]
    active_users: typing.Optional[typing.Set[ActiveUser]]


CollaboratorsT = typing.Dict[
    SeatAccount,
    typing.Dict[SeatRepository, CollaboratorsSetsT],
]


def _get_active_users_key(
    repository: github_types.GitHubRepository,
) -> ActiveUserKeyT:
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
    part1, _, part2 = user.partition("~")
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

    users = {}

    def _add_user(user: github_types.GitHubAccount) -> None:
        if user["id"] == config.BOT_USER_ID:
            return
        elif user["login"] == "web-flow":
            return

        users[user["id"]] = user["login"]

    if event_type == "push":
        typed_event = typing.cast(github_types.GitHubEventPush, event)
    elif event_type == "issue_comment":
        typed_event = typing.cast(github_types.GitHubEventIssueComment, event)
        _add_user(typed_event["issue"]["user"])
        _add_user(typed_event["comment"]["user"])
    elif event_type == "pull_request":
        typed_event = typing.cast(github_types.GitHubEventPullRequest, event)
        _add_user(typed_event["pull_request"]["user"])
        list(map(_add_user, typed_event["pull_request"]["assignees"]))
    elif event_type == "pull_request_review":
        typed_event = typing.cast(github_types.GitHubEventPullRequestReview, event)
        _add_user(typed_event["pull_request"]["user"])
        list(map(_add_user, typed_event["pull_request"]["assignees"]))
        _add_user(typed_event["review"]["user"])
    elif event_type == "pull_request_review_comment":
        typed_event = typing.cast(
            github_types.GitHubEventPullRequestReviewComment, event
        )
        _add_user(typed_event["pull_request"]["user"])
        list(map(_add_user, typed_event["pull_request"]["assignees"]))
        _add_user(typed_event["comment"]["user"])

    if typed_event is None:
        return

    _add_user(typed_event["sender"])

    if not users:
        return

    repo_key = _get_active_users_key(typed_event["repository"])
    transaction = await redis_cache.pipeline()
    for user_id, user_login in users.items():
        user_key = f"{user_id}~{user_login}"
        await transaction.zadd(repo_key, **{user_key: time.time()})

    await transaction.execute()


class SeatCollaboratorJsonT(typing.TypedDict):
    id: int
    login: str


class SeatCollaboratorsJsonT(typing.TypedDict):
    write_users: typing.Optional[typing.List[SeatCollaboratorJsonT]]
    active_users: typing.Optional[typing.List[SeatCollaboratorJsonT]]


class SeatRepositoryJsonT(typing.TypedDict):
    id: int
    name: str
    collaborators: SeatCollaboratorsJsonT


class SeatOrganizationJsonT(typing.TypedDict):
    id: int
    login: str
    repositories: typing.List[SeatRepositoryJsonT]


class SeatsJsonT(typing.TypedDict):
    organizations: typing.List[SeatOrganizationJsonT]


@dataclasses.dataclass
class Seats:
    seats: CollaboratorsT = dataclasses.field(
        default_factory=lambda: collections.defaultdict(
            lambda: collections.defaultdict(
                lambda: {"write_users": None, "active_users": None}
            )
        )
    )

    @classmethod
    async def get(
        cls,
        redis_cache: utils.RedisCache,
        write_users: bool = True,
        active_users: bool = True,
        owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
    ) -> "Seats":
        seats = cls()
        if write_users:
            if owner_id is not None:
                raise RuntimeError("Can't get `write_users` if `owner_id` is set")
            await seats.populate_with_collaborators_with_write_users_access()
        if active_users:
            await seats.populate_with_active_users(redis_cache, owner_id)
        return seats

    def jsonify(self) -> SeatsJsonT:
        data = SeatsJsonT({"organizations": []})
        for org, repos in self.seats.items():
            repos_json = []
            for repo, _seats in repos.items():
                collaborators_json = SeatCollaboratorsJsonT(
                    {
                        "write_users": (
                            None
                            if _seats["write_users"] is None
                            else [
                                {"id": seat.id, "login": seat.login}
                                for seat in _seats["write_users"]
                            ]
                        ),
                        "active_users": (
                            None
                            if _seats["active_users"] is None
                            else [
                                {"id": seat.id, "login": seat.login}
                                for seat in _seats["active_users"]
                            ]
                        ),
                    }
                )
                repos_json.append(
                    SeatRepositoryJsonT(
                        {
                            "id": repo.id,
                            "name": repo.name,
                            "collaborators": collaborators_json,
                        }
                    )
                )
            data["organizations"].append(
                SeatOrganizationJsonT(
                    {
                        "id": org.id,
                        "login": org.login,
                        "repositories": repos_json,
                    }
                )
            )
        return data

    def count(self) -> SeatsCountResultT:
        all_write_users_collaborators = set()
        all_active_users_collaborators = set()
        for repos in self.seats.values():
            for sets in repos.values():
                if sets["write_users"] is not None:
                    all_write_users_collaborators |= sets["write_users"]
                if sets["active_users"] is not None:
                    all_active_users_collaborators |= sets["active_users"]
        return SeatsCountResultT(
            len(all_write_users_collaborators), len(all_active_users_collaborators)
        )

    async def populate_with_active_users(
        self,
        redis_cache: utils.RedisCache,
        owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
    ) -> None:
        async for key in get_active_users_keys(
            redis_cache, owner_id="*" if owner_id is None else owner_id
        ):
            _, _owner_id, owner_login, repo_id, repo_name = key.split("~")
            org = SeatAccount(
                github_types.GitHubAccountIdType(int(_owner_id)),
                github_types.GitHubLogin(owner_login),
            )
            repo = SeatRepository(
                github_types.GitHubRepositoryIdType(int(repo_id)),
                github_types.GitHubRepositoryName(repo_name),
            )
            active_users = await get_active_users(redis_cache, key)

            repo_seats = self.seats[org][repo]
            if repo_seats["active_users"] is None:
                repo_seats["active_users"] = active_users
            else:
                repo_seats["active_users"] |= active_users

    async def populate_with_collaborators_with_write_users_access(self) -> None:
        async with github.AsyncGithubClient(
            auth=github_app.GithubBearerAuth(),
        ) as app_client:
            async for installation in app_client.items("/app/installations"):
                installation = typing.cast(
                    github_types.GitHubInstallation, installation
                )
                org = SeatAccount(
                    installation["account"]["id"], installation["account"]["login"]
                )
                async with github.aget_client(org.id) as client:
                    async for repository in client.items(
                        "/installation/repositories", list_items="repositories"
                    ):
                        repository = typing.cast(
                            github_types.GitHubRepository, repository
                        )
                        repo = SeatRepository(repository["id"], repository["name"])
                        async for collaborator in client.items(
                            f"{repository['url']}/collaborators"
                        ):
                            if collaborator["permissions"]["push"]:
                                seat = SeatAccount(
                                    collaborator["id"], collaborator["login"]
                                )
                                repo_seats = self.seats[org][repo]
                                if repo_seats["write_users"] is None:
                                    repo_seats["write_users"] = {seat}
                                else:
                                    repo_seats["write_users"].add(seat)


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
                    "write_users": seats.write_users,
                    "active_users": seats.active_users,
                    # Deprecated version
                    "seats": seats.write_users,
                },
            )
        except Exception as exc:
            if exceptions.should_be_ignored(exc):
                return
            elif exceptions.need_retry(exc):
                raise tenacity.TryAgain
            else:
                raise


async def count_and_send(redis_cache: utils.RedisCache) -> None:
    await asyncio.sleep(HOUR)
    while True:
        # NOTE(sileht): We loop even if SUBSCRIPTION_TOKEN is missing to not
        # break `tox -e test`. And we can et SUBSCRIPTION_TOKEN to test the
        # daemon with `tox -etest`
        if config.SUBSCRIPTION_TOKEN is None:
            LOG.info("on-premise subscription token missing, nothing to do.")
        else:
            try:
                seats = (await Seats.get(redis_cache)).count()
            except Exception:
                LOG.error("failed to count seats", exc_info=True)
            else:
                try:
                    await send_seats(seats)
                except Exception:
                    LOG.error("failed to send seats usage", exc_info=True)
            LOG.info("reported seats usage", seats=seats)

        await asyncio.sleep(12 * HOUR)


async def report(args: argparse.Namespace) -> None:
    redis_cache = utils.create_aredis_for_cache()
    if args.daemon:
        service.setup("count-seats")
        await count_and_send(redis_cache)
    else:
        service.setup("count-seats", dump_config=False)
        if config.SUBSCRIPTION_TOKEN is None:
            LOG.error("on-premise subscription token missing")
        else:
            seats = await Seats.get(redis_cache)
            if args.json:
                print(json.dumps(seats.jsonify()))
            else:
                seats_count = seats.count()
                LOG.info(
                    "collaborators with write_users access: %s", seats_count.write_users
                )
                LOG.info("active_users collaborators: %s", seats_count.active_users)


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
    return asyncio.run(report(parser.parse_args()))
