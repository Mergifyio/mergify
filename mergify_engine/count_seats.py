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
import datetime
import typing

import daiquiri
import tenacity

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import logs
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

HOUR = datetime.timedelta(hours=1).total_seconds()


CollaboratorsT = typing.Dict[
    github_types.GitHubLogin,  # org name
    typing.Dict[
        github_types.GitHubRepositoryName, typing.Set[github_types.GitHubLogin]
    ],
]


def count_seats(collaborators: CollaboratorsT) -> int:
    all_collaborators = set()
    for repos in collaborators.values():
        for repo_collabs in repos.values():
            all_collaborators |= repo_collabs
    return len(all_collaborators)


async def get_collaborators() -> CollaboratorsT:
    all_collaborators: CollaboratorsT = collections.defaultdict(
        lambda: collections.defaultdict(set)
    )
    async with github.AsyncGithubClient(
        auth=github_app.GithubBearerAuth(),
    ) as app_client:
        async for installation in app_client.items("/app/installations"):
            installation = typing.cast(github_types.GitHubInstallation, installation)
            orgname = installation["account"]["login"]
            async with github.aget_client(
                orgname, installation["account"]["id"]
            ) as client:
                async for repository in client.items(
                    "/installation/repositories", list_items="repositories"
                ):
                    repository = typing.cast(github_types.GitHubRepository, repository)
                    async for collaborator in client.items(
                        f"{repository['url']}/collaborators"
                    ):
                        if collaborator["permissions"]["push"]:
                            all_collaborators[orgname][repository["name"]].add(
                                collaborator["login"]
                            )

    return all_collaborators


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=0.2),
    stop=tenacity.stop_after_attempt(5),
    reraise=True,
)
async def send_seats(seats: int) -> None:
    async with http.AsyncClient() as client:
        try:
            await client.post(
                f"{config.SUBSCRIPTION_BASE_URL}/on-premise/report",
                headers={"Authorization": f"token {config.SUBSCRIPTION_TOKEN}"},
                json={"seats": seats},
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Report used seats")
    parser.add_argument(
        "--daemon",
        "-d",
        action="store_true",
        help="Run as daemon and report usage regularly",
    )
    args = parser.parse_args()

    if args.daemon:
        logs.setup_logging()
        asyncio.run(count_and_send())
    else:
        logs.setup_logging(dump_config=False)
        if config.SUBSCRIPTION_TOKEN is None:
            LOG.error("on-premise subscription token missing")
        else:
            collaborators = asyncio.run(get_collaborators())
            seats = count_seats(collaborators)
            LOG.info("collaborators: %s", seats)
