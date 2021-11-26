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


import fastapi
from starlette import responses

from mergify_engine import github_types
from mergify_engine.web import api


router = fastapi.APIRouter(
    tags=["badges"],
)


def _get_badge_url(
    owner: github_types.GitHubLogin,
    repo: github_types.GitHubRepositoryName,
    ext: str,
    style: str,
) -> responses.RedirectResponse:
    return responses.RedirectResponse(
        url=f"https://img.shields.io/endpoint.{ext}?url=https://dashboard.mergify.com/badges/{owner}/{repo}&style={style}",
        status_code=302,
    )


@router.get(
    "/badges/{owner}/{repository}.png",  # noqa: FS003
    summary="Get PNG badge",
    description="Get badge in PNG image format",
    response_class=fastapi.Response,  # to drop application/json from openapi media type
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "Not found"},
        200: {
            "content": {"image/png": {}},
            "description": "An PNG image.",
        },
    },
)
async def badge_png(
    owner: github_types.GitHubLogin = fastapi.Path(  # noqa: B008
        ..., description="The owner of the repository"
    ),
    repository: github_types.GitHubRepositoryName = fastapi.Path(  # noqa: B008
        ..., description="The name of the repository"
    ),
    style: str = fastapi.Query(  # noqa: B008
        default="flat",
        description="The style of the button, more details on https://shields.io/.",
    ),
) -> responses.RedirectResponse:  # pragma: no cover
    return _get_badge_url(owner, repository, "png", style)


@router.get(
    "/badges/{owner}/{repository}.svg",  # noqa: FS003
    summary="Get SVG badge",
    description="Get badge in SVG image format",
    response_class=fastapi.Response,  # to drop application/json from openapi media type
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "Not found"},
        200: {
            "content": {"image/png": {}},
            "description": "An SVG image.",
        },
    },
)
async def badge_svg(
    owner: github_types.GitHubLogin = fastapi.Path(  # noqa: B008
        ..., description="The owner of the repository"
    ),
    repository: github_types.GitHubRepositoryName = fastapi.Path(  # noqa: B008
        ..., description="The name of the repository"
    ),
    style: str = fastapi.Query(  # noqa: B008
        default="flat",
        description="The style of the button, more details on https://shields.io/.",
    ),
) -> responses.RedirectResponse:  # pragma: no cover
    return _get_badge_url(owner, repository, "svg", style)


@router.get(
    "/badges/{owner}/{repository}",  # noqa: FS003
    summary="Get shields.io badge config",
    description="Get shields.io badge JSON configuration",
    response_class=fastapi.Response,  # Allow to not document the shields.io format
    responses={
        **api.default_responses,  # type: ignore
        404: {"description": "Not found"},
        200: {
            "content": {"application/json": {}},
            "description": "The shields.io badge JSON configuration",
        },
    },
)
async def badge(
    owner: github_types.GitHubLogin = fastapi.Path(  # noqa: B008
        ..., description="The owner of the repository"
    ),
    repository: github_types.GitHubRepositoryName = fastapi.Path(  # noqa: B008
        ..., description="The name of the repository"
    ),
) -> responses.RedirectResponse:
    return responses.RedirectResponse(
        url=f"https://dashboard.mergify.com/badges/{owner}/{repository}"
    )
