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
import daiquiri
import fastapi
import pydantic

from mergify_engine import github_types
from mergify_engine.dashboard import application as application_mod
from mergify_engine.web import api
from mergify_engine.web.api import security


LOG = daiquiri.getLogger(__name__)


@pydantic.dataclasses.dataclass
class GitHubAccount:
    id: github_types.GitHubAccountIdType
    login: github_types.GitHubLogin
    type: github_types.GitHubAccountType


@pydantic.dataclasses.dataclass
class ApplicationResponse:
    id: int
    name: str
    github_account: GitHubAccount


router = fastapi.APIRouter()


@router.get(
    "/application",  # noqa: FS003
    summary="",
    description="Get current authenticated application",
    tags=["applications"],
    response_model=ApplicationResponse,
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "Not found"},
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "id": 123456,
                        "name": "an application name",
                        "github_account": {
                            "id": 123456,
                            "login": "Mergifyio",
                            "type": "Organization",
                        },
                    }
                }
            }
        },
    },
)
async def application(
    application: application_mod.Application = fastapi.Depends(  # noqa: B008
        security.get_application
    ),
    installation: github_types.GitHubInstallation = fastapi.Depends(  # noqa: B008
        security.get_installation
    ),
) -> ApplicationResponse:
    return ApplicationResponse(
        id=application.id,
        name=application.name,
        github_account=GitHubAccount(
            installation["account"]["id"],
            installation["account"]["login"],
            installation["account"]["type"],
        ),
    )
