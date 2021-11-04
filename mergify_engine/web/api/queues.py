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
import dataclasses
import datetime
import typing

import daiquiri
import fastapi
import pydantic

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.queue import merge_train
from mergify_engine.web import api
from mergify_engine.web import redis
from mergify_engine.web.api import security


LOG = daiquiri.getLogger(__name__)


@pydantic.dataclasses.dataclass
class Branch:
    name: github_types.GitHubRefType = dataclasses.field(
        metadata={"description": "The name of the branch"}
    )


@pydantic.dataclasses.dataclass
class SpeculativeCheckPullRequest:
    in_place: bool = dataclasses.field(
        metadata={"description": "Whether the pull request has been checked in-place"}
    )
    number: github_types.GitHubPullRequestNumber = dataclasses.field(
        metadata={
            "description": "The number of the pull request used by the speculative check"
        }
    )


@pydantic.dataclasses.dataclass
class QueueRule:
    name: rules.QueueName = dataclasses.field(
        metadata={"description": "The name of the queue rule"}
    )

    config: rules.QueueConfig = dataclasses.field(
        metadata={"description": "The configuration of the queue rule"}
    )


@pydantic.dataclasses.dataclass
class PullRequestQueued:
    number: github_types.GitHubPullRequestNumber = dataclasses.field(
        metadata={"description": "The number of the pull request"}
    )

    position: int = dataclasses.field(
        metadata={"description": "The position of the pull request in the queue"}
    )

    priority: int = dataclasses.field(
        metadata={"description": "The priority of this pull request"}
    )
    queue_rule: QueueRule = dataclasses.field(
        metadata={"description": "The queue rule associated to this pull request"}
    )

    queued_at: datetime.datetime = dataclasses.field(
        metadata={
            "description": "The timestamp when the pull requested has entered in the queue"
        }
    )

    speculative_check_pull_request: typing.Optional[SpeculativeCheckPullRequest]


@pydantic.dataclasses.dataclass
class Queue:
    branch: Branch = dataclasses.field(
        metadata={"description": "The branch of this queue"}
    )

    pull_requests: typing.List[PullRequestQueued] = dataclasses.field(
        default_factory=list,
        metadata={"description": "The pull requests in this queue"},
    )


@pydantic.dataclasses.dataclass
class Queues:
    queues: typing.List[Queue] = dataclasses.field(
        default_factory=list, metadata={"description": "The queues of the repository"}
    )


router = fastapi.APIRouter()


@router.get(
    "/repos/{owner}/{repository}/queues",  # noqa: FS003
    summary="Get merge queues",
    description="Get the list of pull requests queued in a merge queue of a repository",
    tags=["queues"],
    response_model=Queues,
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "Not found"},
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "queues": [
                            {
                                "branch": {"name": "main"},
                                "pull_requests": [
                                    {
                                        "number": 5678,
                                        "position": 1,
                                        "priority": 100,
                                        "queue_rule": {
                                            "name": "default",
                                            "config": {
                                                "priority": 100,
                                                "batch_size": 1,
                                                "speculative_checks": 2,
                                                "allow_inplace_speculative_checks": True,
                                            },
                                        },
                                        "speculative_check_pull_request": {
                                            "in_place": True,
                                            "number": 5678,
                                        },
                                        "queued_at": "2021-10-14T14:19:12",
                                    },
                                    {
                                        "number": 4242,
                                        "position": 1,
                                        "priority": 100,
                                        "queue_rule": {
                                            "name": "default",
                                            "config": {
                                                "priority": 100,
                                                "batch_size": 1,
                                                "speculative_checks": 2,
                                                "allow_inplace_speculative_checks": True,
                                            },
                                        },
                                        "speculative_check_pull_request": {
                                            "in_place": False,
                                            "number": 7899,
                                        },
                                        "queued_at": "2021-10-14T14:19:12",
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        },
    },
)
async def repository_queues(
    owner: github_types.GitHubLogin = fastapi.Path(  # noqa: B008
        ..., description="The owner of the repository"
    ),
    repository: github_types.GitHubRepositoryName = fastapi.Path(  # noqa: B008
        ..., description="The name of the repository"
    ),
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
    installation_json: github_types.GitHubInstallation = fastapi.Depends(  # noqa: B008
        security.get_installation
    ),
) -> Queues:
    async with github.aget_client(installation_json) as client:
        try:
            # Check this token as access to this repository
            repo = typing.cast(
                github_types.GitHubRepository,
                await client.item(f"/repos/{owner}/{repository}"),
            )
        except (http.HTTPNotFound, http.HTTPForbidden, http.HTTPUnauthorized):
            LOG.error("404 REPO")
            raise fastapi.HTTPException(status_code=404)

        sub = await subscription.Subscription.get_subscription(
            redis_cache, repo["owner"]["id"]
        )
        installation = context.Installation(installation_json, sub, client, redis_cache)
        repository_ctxt = installation.get_repository_from_github_data(repo)

        queues = Queues()

        async for train in merge_train.Train.iter_trains(installation, repository_ctxt):
            await train.load()

            queue_rules = await train.get_queue_rules()
            if queue_rules is None:
                # The train is going the be deleted, so skip it.
                continue

            queue = Queue(Branch(train.ref))
            for position, (embarked_pull, car) in enumerate(
                train._iter_embarked_pulls()
            ):
                if car is None:
                    speculative_check_pull_request = None
                elif car.creation_state == "updated":
                    speculative_check_pull_request = SpeculativeCheckPullRequest(
                        in_place=True, number=embarked_pull.user_pull_request_number
                    )
                elif car.creation_state == "created":
                    if car.queue_pull_request_number is None:
                        raise RuntimeError(
                            "car state is created, but queue_pull_request_number is None"
                        )
                    speculative_check_pull_request = SpeculativeCheckPullRequest(
                        in_place=False, number=car.queue_pull_request_number
                    )
                else:
                    raise RuntimeError(
                        f"Car creation state unknown: {car.creation_state}"
                    )

                try:
                    queue_rule = queue_rules[embarked_pull.config["name"]]
                except KeyError:
                    # This car is going to be deleted so skip it
                    continue
                queue.pull_requests.append(
                    PullRequestQueued(
                        embarked_pull.user_pull_request_number,
                        position,
                        embarked_pull.config["priority"],
                        QueueRule(
                            name=embarked_pull.config["name"], config=queue_rule.config
                        ),
                        embarked_pull.queued_at,
                        speculative_check_pull_request,
                    )
                )

            queues.queues.append(queue)

        return queues
