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
from starlette.status import HTTP_204_NO_CONTENT

from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.dashboard import application as application_mod
from mergify_engine.queue import freeze
from mergify_engine.queue import merge_train
from mergify_engine.rules import InvalidRules
from mergify_engine.rules import get_mergify_config
from mergify_engine.web import api
from mergify_engine.web.api import security


LOG = daiquiri.getLogger(__name__)

router = fastapi.APIRouter(
    tags=["queues"],
    dependencies=[
        fastapi.Depends(security.require_authentication),
    ],
)


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
    started_at: datetime.datetime = dataclasses.field(
        metadata={
            "description": "The timestamp when the checks has started for this pull request"
        }
    )
    ended_at: typing.Optional[datetime.datetime] = dataclasses.field(
        metadata={
            "description": "The timestamp when the checks has ended for this pull request"
        }
    )
    checks: typing.List[merge_train.QueueCheck] = dataclasses.field(
        metadata={"description": "The list of pull request checks"}
    )
    evaluated_conditions: typing.Optional[str] = dataclasses.field(
        metadata={"description": "The queue rule conditions evaluation report"}
    )
    state: merge_train.CheckStateT = dataclasses.field(
        metadata={"description": "The global state of the checks"}
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
class QueuesConfig:
    configuration: typing.List[QueueRule] = dataclasses.field(
        default_factory=list,
        metadata={"description": "The queues configuration of the repository"},
    )


@pydantic.dataclasses.dataclass
class Queues:
    queues: typing.List[Queue] = dataclasses.field(
        default_factory=list, metadata={"description": "The queues of the repository"}
    )


# FIXME(sileht): reuse dataclasses variante once
# https://github.com/tiangolo/fastapi/issues/4679 is fixed
class QueueFreezePayload(pydantic.BaseModel):
    reason: str = pydantic.Field(
        max_length=255, description="The reason of the queue freeze"
    )


@pydantic.dataclasses.dataclass
class QueueFreeze:
    application_name: str = dataclasses.field(
        metadata={"description": "Application name responsible for the freeze"},
    )
    application_id: int = dataclasses.field(
        metadata={"description": "Application ID responsible for the freeze"},
    )
    name: str = dataclasses.field(
        default_factory=str, metadata={"description": "Queue name"}
    )
    reason: str = dataclasses.field(
        default_factory=str, metadata={"description": "The reason of the queue freeze"}
    )
    freeze_date: datetime.datetime = dataclasses.field(
        default_factory=date.utcnow,
        metadata={"description": "The date and time of the freeze"},
    )


@pydantic.dataclasses.dataclass
class QueueFreezeResponse:
    queue_freezes: typing.List[QueueFreeze] = dataclasses.field(
        default_factory=list,
        metadata={"description": "The frozen queues of the repository"},
    )


@router.get(
    "/repos/{owner}/{repository}/queues",  # noqa: FS003
    summary="Get merge queues",
    description="Get the list of pull requests queued in a merge queue of a repository",
    response_model=Queues,
    responses={
        **api.default_responses,  # type: ignore
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
                                                "batch_max_wait_time": 0,
                                                "speculative_checks": 2,
                                                "allow_inplace_checks": True,
                                                "disallow_checks_interruption_from_queues": [],
                                                "checks_timeout": 60,
                                                "draft_bot_account": "",
                                            },
                                        },
                                        "speculative_check_pull_request": {
                                            "in_place": True,
                                            "number": 5678,
                                            "started_at": "2021-10-14T14:19:12+00:00",
                                            "ended_at": "2021-10-14T15:00:42+00:00",
                                            "checks": [],
                                            "evaluated_conditions": "",
                                            "state": "success",
                                        },
                                        "queued_at": "2021-10-14T14:19:12+00:00",
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
                                                "batch_max_wait_time": 0,
                                                "speculative_checks": 2,
                                                "allow_inplace_checks": True,
                                                "disallow_checks_interruption_from_queues": [],
                                                "checks_timeout": 60,
                                                "draft_bot_account": "",
                                            },
                                        },
                                        "speculative_check_pull_request": {
                                            "in_place": False,
                                            "number": 7899,
                                            "started_at": "2021-10-14T14:19:12+00:00",
                                            "ended_at": "2021-10-14T15:00:42+00:00",
                                            "checks": [],
                                            "evaluated_conditions": "",
                                            "state": "success",
                                        },
                                        "queued_at": "2021-10-14T14:19:12+00:00",
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
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> Queues:
    queues = Queues()
    async for train in merge_train.Train.iter_trains(repository_ctxt):
        queue_rules = await train.get_queue_rules()
        if queue_rules is None:
            # The train is going the be deleted, so skip it.
            continue

        queue = Queue(Branch(train.ref))
        for position, (embarked_pull, car) in enumerate(train._iter_embarked_pulls()):
            if car is None:
                speculative_check_pull_request = None
            elif car.creation_state in ["created", "updated"]:
                if car.queue_pull_request_number is None:
                    raise RuntimeError(
                        f"car state is {car.creation_state}, but queue_pull_request_number is None"
                    )
                speculative_check_pull_request = SpeculativeCheckPullRequest(
                    in_place=car.creation_state == "updated",
                    number=car.queue_pull_request_number,
                    started_at=car.creation_date,
                    ended_at=car.checks_ended_timestamp,
                    state=car.checks_conclusion.value or "pending",
                    checks=car.last_checks,
                    evaluated_conditions=car.last_evaluated_conditions,
                )
            elif car.creation_state in ("failed", "pending"):
                speculative_check_pull_request = None
            else:
                raise RuntimeError(f"Car creation state unknown: {car.creation_state}")

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


@router.get(
    "/repos/{owner}/{repository}/queues/configuration",  # noqa: FS003
    summary="Get merge queues configuration",
    description="Get the list of all merge queues configuration sorted by processing order",
    response_model=QueuesConfig,
    responses={
        **api.default_responses,  # type: ignore
        422: {"description": "The configuration file is invalid."},
    },
)
async def repository_queues_configuration(
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> QueuesConfig:

    config_file = await repository_ctxt.get_mergify_config_file()
    if config_file is None:
        return QueuesConfig()

    try:
        config = get_mergify_config(config_file)
    except InvalidRules:
        raise fastapi.HTTPException(
            status_code=422,
            detail="The configuration file is invalid.",
        )

    return QueuesConfig(
        [
            QueueRule(
                config=rule.config,
                name=rule.name,
            )
            for rule in config["queue_rules"]
        ]
    )


@router.put(
    "/repos/{owner}/{repository}/queue/{queue_name}/freeze",  # noqa: FS003
    summary="Freezes merge queue",
    description="Freezes the merge of the requested queue and the queues following it",
    response_model=QueueFreezeResponse,
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "The queue does not exist"},
    },
    dependencies=[fastapi.Depends(security.check_subscription_feature_queue_freeze)],
)
async def create_queue_freeze(
    queue_freeze_payload: QueueFreezePayload,
    application: application_mod.Application = fastapi.Depends(  # noqa: B008
        security.get_application
    ),
    queue_name: rules.QueueName = fastapi.Path(  # noqa: B008
        ..., description="The name of the queue"
    ),
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> QueueFreezeResponse:

    if queue_freeze_payload.reason == "":
        queue_freeze_payload.reason = "No freeze reason was specified."

    config_file = await repository_ctxt.get_mergify_config_file()
    if config_file is None:
        raise fastapi.HTTPException(
            status_code=404, detail="Mergify configuration file is missing."
        )

    config = get_mergify_config(config_file)
    queue_rules = config["queue_rules"]
    if all(queue_name != rule.name for rule in queue_rules):
        raise fastapi.HTTPException(
            status_code=404, detail=f'The queue "{queue_name}" does not exist.'
        )

    qf = await freeze.QueueFreeze.get(repository_ctxt, queue_name)
    if qf is None:
        qf = freeze.QueueFreeze(
            repository=repository_ctxt,
            name=queue_name,
            reason=queue_freeze_payload.reason,
            application_name=application.name,
            application_id=application.id,
            freeze_date=date.utcnow(),
        )
        await qf.save()

    elif qf.reason != queue_freeze_payload.reason:
        qf.reason = queue_freeze_payload.reason
        await qf.save()

    return QueueFreezeResponse(
        queue_freezes=[
            QueueFreeze(
                name=qf.name,
                reason=qf.reason,
                application_name=qf.application_name,
                application_id=qf.application_id,
                freeze_date=qf.freeze_date,
            )
        ],
    )


@router.delete(
    "/repos/{owner}/{repository}/queue/{queue_name}/freeze",  # noqa: FS003
    summary="Unfreeze merge queue",
    description="Unfreeze the specified merge queue",
    dependencies=[fastapi.Depends(security.check_subscription_feature_queue_freeze)],
    status_code=204,
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "The queue does not exist or is not currently frozen"},
    },
)
async def delete_queue_freeze(
    application: application_mod.Application = fastapi.Depends(  # noqa: B008
        security.get_application
    ),
    queue_name: rules.QueueName = fastapi.Path(  # noqa: B008
        ..., description="The name of the queue"
    ),
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> fastapi.Response:

    qf = freeze.QueueFreeze(
        repository=repository_ctxt,
        name=queue_name,
        application_name=application.name,
        application_id=application.id,
    )
    if not await qf.delete():
        raise fastapi.HTTPException(
            status_code=404,
            detail=f'The queue "{queue_name}" does not exist or is not currently frozen.',
        )

    return fastapi.Response(status_code=HTTP_204_NO_CONTENT)


@router.get(
    "/repos/{owner}/{repository}/queue/{queue_name}/freeze",  # noqa: FS003
    summary="Get queue freeze data",
    description="Checks if the queue is frozen and get the queue freeze data",
    response_model=QueueFreezeResponse,
    dependencies=[fastapi.Depends(security.check_subscription_feature_queue_freeze)],
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "The queue does not exist or is not currently frozen"},
    },
)
async def get_queue_freeze(
    queue_name: rules.QueueName = fastapi.Path(  # noqa: B008
        ..., description="The name of the queue"
    ),
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> QueueFreezeResponse:

    qf = await freeze.QueueFreeze.get(repository_ctxt, queue_name)
    if qf is None:
        raise fastapi.HTTPException(
            status_code=404,
            detail=f'The queue "{queue_name}" does not exist or is not currently frozen.',
        )

    return QueueFreezeResponse(
        queue_freezes=[
            QueueFreeze(
                name=qf.name,
                reason=qf.reason,
                application_name=qf.application_name,
                application_id=qf.application_id,
                freeze_date=qf.freeze_date,
            )
        ],
    )


@router.get(
    "/repos/{owner}/{repository}/queues/freezes",  # noqa: FS003
    summary="Get the list of frozen queues",
    description="Get the list of frozen queues inside the requested repository",
    response_model=QueueFreezeResponse,
    dependencies=[fastapi.Depends(security.check_subscription_feature_queue_freeze)],
    responses={
        **api.default_responses,  # type: ignore
    },
)
async def get_list_queue_freeze(
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
) -> QueueFreezeResponse:

    return QueueFreezeResponse(
        queue_freezes=[
            QueueFreeze(
                name=qf.name,
                reason=qf.reason,
                application_name=qf.application_name,
                application_id=qf.application_id,
                freeze_date=qf.freeze_date,
            )
            async for qf in freeze.QueueFreeze.get_all(repository_ctxt)
        ]
    )
