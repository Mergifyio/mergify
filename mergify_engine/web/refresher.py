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
import voluptuous

from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.web import auth
from mergify_engine.web import redis


router = fastapi.APIRouter()


@router.post(
    "/refresh/{owner_login}/{repo_name}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_repo(
    owner_login: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    installation_json = await github.get_installation_from_login(owner_login)
    async with github.aget_client(installation_json) as client:
        try:
            repository = await client.item(f"/repos/{owner_login}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_repository_refresh(
        redis_links.stream, repository, action="user", source="API"
    )
    return responses.Response("Refresh queued", status_code=202)


RefreshActionSchema = voluptuous.Schema(voluptuous.Any("user", "admin", "internal"))


@router.post(
    "/refresh/{owner_login}/{repo_name}/pull/{pull_request_number}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_pull(
    owner_login: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    pull_request_number: github_types.GitHubPullRequestNumber,
    action: github_types.GitHubEventRefreshActionType = "user",
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    action = RefreshActionSchema(action)

    installation_json = await github.get_installation_from_login(owner_login)
    async with github.aget_client(installation_json) as client:
        try:
            repository = await client.item(f"/repos/{owner_login}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_pull_refresh(
        redis_links.stream,
        repository,
        action=action,
        pull_request_number=pull_request_number,
        source="API",
    )
    return responses.Response("Refresh queued", status_code=202)


@router.post(
    "/refresh/{owner_login}/{repo_name}/branch/{branch}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_branch(
    owner_login: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    branch: str,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    installation_json = await github.get_installation_from_login(owner_login)
    async with github.aget_client(installation_json) as client:
        try:
            repository = await client.item(f"/repos/{owner_login}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_branch_refresh(
        redis_links.stream,
        repository,
        action="user",
        source="API",
        ref=github_types.GitHubRefType(f"refs/heads/{branch}"),
    )
    return responses.Response("Refresh queued", status_code=202)
