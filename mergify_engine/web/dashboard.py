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

import typing

import fastapi
from starlette import requests
from starlette import responses

from mergify_engine import count_seats
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.dashboard import application
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.usage import last_seen
from mergify_engine.web import auth
from mergify_engine.web import redis


router = fastapi.APIRouter()


@router.get(
    "/organization/{owner_id}/usage",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def get_stats(
    owner_id: github_types.GitHubAccountIdType,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    last_seen_at = await last_seen.get(redis_links.cache, owner_id)
    seats = await count_seats.Seats.get(
        redis_links.active_users, write_users=False, owner_id=owner_id
    )
    data = seats.jsonify()
    if data["organizations"]:
        if len(data["organizations"]) > 1:
            raise RuntimeError(
                "count_seats.Seats.get() returns more than one organization"
            )
        repos = data["organizations"][0]["repositories"]
    else:
        repos = []

    return responses.JSONResponse(
        {
            "repositories": repos,
            "last_seen_at": None if last_seen_at is None else last_seen_at.isoformat(),
        }
    )


@router.put(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def subscription_cache_update(
    owner_id: github_types.GitHubAccountIdType,
    request: requests.Request,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    sub = await request.json()
    if sub is None:
        return responses.Response("Empty content", status_code=400)
    try:
        await subscription.Subscription.update_subscription(
            redis_links.cache, int(owner_id), sub
        )
    except NotImplementedError:
        return responses.Response("Updating subscription is disabled", status_code=400)

    return responses.Response("Cache updated", status_code=200)


@router.delete(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def subscription_cache_delete(
    owner_id: github_types.GitHubAccountIdType,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    try:
        await subscription.Subscription.delete_subscription(redis_links.cache, owner_id)
    except NotImplementedError:
        return responses.Response("Deleting subscription is disabled", status_code=400)
    return responses.Response("Cache cleaned", status_code=200)


@router.delete(
    "/tokens-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def tokens_cache_delete(
    owner_id: github_types.GitHubAccountIdType,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    try:
        await user_tokens.UserTokens.delete(redis_links.cache, owner_id)
    except NotImplementedError:
        return responses.Response("Deleting tokens is disabled", status_code=400)
    return responses.Response("Cache cleaned", status_code=200)


@router.put(
    "/application/{api_access_key}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def application_cache_update(
    api_access_key: str,
    request: requests.Request,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    data = typing.cast(
        typing.Optional[application.ApplicationDashboardJSON], await request.json()
    )
    if data is None:
        return responses.Response("Empty content", status_code=400)

    try:
        await application.Application.update(redis_links.cache, api_access_key, data)
    except NotImplementedError:
        return responses.Response("Updating application is disabled", status_code=400)

    return responses.Response("Cache updated", status_code=200)


@router.delete(
    "/application/{api_access_key}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.dashboard)],
)
async def application_cache_delete(
    api_access_key: str,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    try:
        await application.Application.delete(redis_links.cache, api_access_key)
    except NotImplementedError:
        return responses.Response("Deleting subscription is disabled", status_code=400)
    return responses.Response("Cache cleaned", status_code=200)
