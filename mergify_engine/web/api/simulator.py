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

import daiquiri
import fastapi
import pydantic

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner
from mergify_engine.web import api
from mergify_engine.web.api import security


LOG = daiquiri.getLogger(__name__)


router = fastapi.APIRouter(
    tags=["simulator"],
    dependencies=[
        fastapi.Depends(security.require_authentication),
    ],
)


class SimulatorPayload(pydantic.BaseModel):
    mergify_yml: str = pydantic.Field(description="A Mergify configuration")

    def get_config(self) -> rules.MergifyConfig:
        try:
            return rules.get_mergify_config(
                context.MergifyConfigFile(
                    {
                        "type": "file",
                        "content": "whatever",
                        "sha": github_types.SHAType("whatever"),
                        "path": ".mergify.yml",
                        "decoded_content": self.mergify_yml,
                    }
                )
            )
        except rules.InvalidRules as exc:
            detail = [
                {
                    "loc": ("body", "mergify_yml"),
                    "msg": rules.InvalidRules.format_error(e),
                    "type": "mergify_config_error",
                }
                for e in sorted(exc.errors, key=str)
            ]
            raise fastapi.HTTPException(status_code=422, detail=detail)


@pydantic.dataclasses.dataclass
class SimulatorResponse:
    title: str = dataclasses.field(
        metadata={"description": "The title of the Mergify check run simulation"},
    )
    summary: str = dataclasses.field(
        metadata={"description": "The summary of the Mergify check run simulation"},
    )


@router.post(
    "/repos/{owner}/{repository}/pulls/{number}/simulator",  # noqa: FS003
    summary="Get a Mergify simulation for a pull request",
    description="Get a simulation of what Mergify will do on a pull request",
    response_model=SimulatorResponse,
    responses={
        **api.default_responses,  # type: ignore
    },
)
async def simulator_pull(
    body: SimulatorPayload,  # noqa: B008
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        security.get_repository_context
    ),
    number: int = fastapi.Path(  # noqa: B008
        ..., description="The pull request number"
    ),
) -> SimulatorResponse:
    config = body.get_config()
    try:
        ctxt = await repository_ctxt.get_pull_request_context(
            github_types.GitHubPullRequestNumber(number)
        )
    except http.HTTPClientSideError as e:
        raise fastapi.HTTPException(status_code=e.status_code, detail=e.message)
    ctxt.sources = [{"event_type": "mergify-simulator", "data": [], "timestamp": ""}]  # type: ignore[typeddict-item]
    match = await config["pull_request_rules"].get_pull_request_rule(ctxt)
    title, summary = await actions_runner.gen_summary(
        ctxt, config["pull_request_rules"], match
    )
    return SimulatorResponse(
        title=title,
        summary=summary,
    )


@router.post(
    "/repos/{owner}/{repository}/simulator",  # noqa: FS003
    summary="Get a Mergify simulation for a repository",
    description="Get a simulation of what Mergify will do for this repository",
    response_model=SimulatorResponse,
    # checkout repository permissions
    dependencies=[fastapi.Depends(security.get_repository_context)],
    responses={
        **api.default_responses,  # type: ignore
    },
)
async def simulator_repo(
    body: SimulatorPayload,  # noqa: B008
) -> SimulatorResponse:
    body.get_config()
    return SimulatorResponse(
        title="The configuration is valid",
        summary="",
    )
