# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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
import typing

import daiquiri
import fastapi
import pydantic

from mergify_engine import context
from mergify_engine import eventlogs
from mergify_engine import github_types
from mergify_engine.web import api
from mergify_engine.web.api import security


LOG = daiquiri.getLogger(__name__)


router = fastapi.APIRouter(
    tags=["eventlogs"],
    dependencies=[
        fastapi.Depends(security.require_authentication),
    ],
)


Event = typing.Annotated[
    typing.Union[
        eventlogs.EventLabel, eventlogs.EventRefresh, eventlogs.EventDismissReview
    ],
    pydantic.Field(discriminator="event"),
]


@pydantic.dataclasses.dataclass
class EventLogsResponse:
    events: typing.List[Event] = dataclasses.field(
        default_factory=list,
        metadata={
            "description": "The list of events of a pull request",
        },
    )


@router.get(
    "/repos/{owner}/{repository}/pulls/{pull}/events",  # noqa: FS003
    summary="Get the event logs of a pull request",
    description="Get the event logs of the requested pull request",
    response_model=EventLogsResponse,
    dependencies=[fastapi.Depends(security.check_subscription_feature_eventlogs)],
    responses={
        **api.default_responses,  # type: ignore
    },
)
async def get_pull_request_eventlogs(
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
    pull: github_types.GitHubPullRequestNumber = fastapi.Path(  # noqa: B008
        ..., description="Pull request number"
    ),
) -> EventLogsResponse:
    # TODO(sileht): add pagination
    return EventLogsResponse(
        events=[e async for e in eventlogs.get(repository_ctxt, pull)]
    )
