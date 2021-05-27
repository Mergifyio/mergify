# -*- encoding: utf-8 -*-
#
# Copyright © 2019–2021 Mergify SAS
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
import collections
import typing

import aredis
import daiquiri
from datadog import statsd
import fastapi
import httpx
from starlette import requests
from starlette import responses
import voluptuous

from mergify_engine import config
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import subscription
from mergify_engine import user_tokens
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.queue import merge_train
from mergify_engine.web import auth
from mergify_engine.web import badges
from mergify_engine.web import config_validator
from mergify_engine.web import redis
from mergify_engine.web import simulator


LOG = daiquiri.getLogger(__name__)

app = fastapi.FastAPI()
app.mount("/simulator", simulator.app)
app.mount("/validate", config_validator.app)
app.mount("/badges", badges.app)

# Set the maximum timeout to 5 seconds: GitHub is not going to wait for
# more than 10 seconds for us to accept an event, so if we're unable to
# forward an event in 5 seconds, just drop it.
EVENT_FORWARD_TIMEOUT = 5


@app.on_event("startup")
async def startup() -> None:
    await redis.startup()


@app.on_event("shutdown")
async def shutdown() -> None:
    await redis.shutdown()


@app.exception_handler(aredis.exceptions.ConnectionError)
async def redis_errors(
    request: requests.Request, exc: aredis.exceptions.ConnectionError
) -> responses.JSONResponse:
    statsd.increment("redis.client.connection.errors")
    LOG.warning("FastAPI lost Redis connection", exc_info=exc)
    return responses.JSONResponse(status_code=503)


@app.post(
    "/refresh/{owner}/{repo_name}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_repo(
    owner: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
    redis_stream: utils.RedisStream = fastapi.Depends(  # noqa: B008
        redis.get_redis_stream
    ),
) -> responses.Response:
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_refresh(redis_cache, redis_stream, repository)
    return responses.Response("Refresh queued", status_code=202)


RefreshActionSchema = voluptuous.Schema(voluptuous.Any("user", "admin", "internal"))


@app.post(
    "/refresh/{owner}/{repo_name}/pull/{pull_request_number}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_pull(
    owner: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    pull_request_number: github_types.GitHubPullRequestNumber,
    action: github_types.GitHubEventRefreshActionType = "user",
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
    redis_stream: utils.RedisStream = fastapi.Depends(  # noqa: B008
        redis.get_redis_stream
    ),
) -> responses.Response:
    action = RefreshActionSchema(action)
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_refresh(
        redis_cache,
        redis_stream,
        repository,
        pull_request_number=pull_request_number,
        action=action,
    )
    return responses.Response("Refresh queued", status_code=202)


@app.post(
    "/refresh/{owner}/{repo_name}/branch/{branch}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_branch(
    owner: github_types.GitHubLogin,
    repo_name: github_types.GitHubRepositoryName,
    branch: str,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
    redis_stream: utils.RedisStream = fastapi.Depends(  # noqa: B008
        redis.get_redis_stream
    ),
) -> responses.Response:
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await utils.send_refresh(
        redis_cache,
        redis_stream,
        repository,
        ref=github_types.GitHubRefType(f"refs/heads/{branch}"),
    )
    return responses.Response("Refresh queued", status_code=202)


@app.put(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_update(
    owner_id: github_types.GitHubAccountIdType,
    request: requests.Request,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    sub = await request.json()
    if sub is None:
        return responses.Response("Empty content", status_code=400)
    try:
        await subscription.Subscription.update_subscription(
            redis_cache, int(owner_id), sub
        )
    except NotImplementedError:
        return responses.Response("Updating subscription is disabled", status_code=400)

    return responses.Response("Cache updated", status_code=200)


@app.delete(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_delete(
    owner_id: github_types.GitHubAccountIdType,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    try:
        await subscription.Subscription.delete_subscription(redis_cache, owner_id)
    except NotImplementedError:
        return responses.Response("Deleting subscription is disabled", status_code=400)
    return responses.Response("Cache cleaned", status_code=200)


@app.delete(
    "/tokens-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def tokens_cache_delete(
    owner_id: github_types.GitHubAccountIdType,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    try:
        await user_tokens.UserTokens.delete(redis_cache, owner_id)
    except NotImplementedError:
        return responses.Response("Deleting tokens is disabled", status_code=400)
    return responses.Response("Cache cleaned", status_code=200)


@app.post("/marketplace", dependencies=[fastapi.Depends(auth.signature)])
async def marketplace_handler(
    request: requests.Request,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    LOG.info(
        "Marketplace event",
        event_type=event_type,
        event_id=event_id,
        sender=data["sender"]["login"],
        gh_owner=data["marketplace_purchase"]["account"]["login"],
    )

    await subscription.Subscription.delete_subscription(
        redis_cache, data["marketplace_purchase"]["account"]["id"]
    )

    if config.WEBHOOK_MARKETPLACE_FORWARD_URL:
        raw = await request.body()
        try:
            async with http.AsyncClient(timeout=EVENT_FORWARD_TIMEOUT) as client:
                await client.post(
                    config.WEBHOOK_MARKETPLACE_FORWARD_URL,
                    content=raw.decode(),
                    headers={
                        "X-GitHub-Event": event_type,
                        "X-GitHub-Delivery": event_id,
                        "X-Hub-Signature": request.headers.get("X-Hub-Signature"),
                        "User-Agent": request.headers.get("User-Agent"),
                        "Content-Type": request.headers.get("Content-Type"),
                    },
                )
        except httpx.TimeoutException:
            LOG.warning(
                "Fail to forward Marketplace event",
                event_type=event_type,
                event_id=event_id,
                sender=data["sender"]["login"],
                gh_owner=data["marketplace_purchase"]["account"]["login"],
            )

    return responses.Response("Event queued", status_code=202)


@app.get(
    "/queues/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def queues(
    owner_id: github_types.GitHubAccountIdType,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    queues: typing.Dict[
        str, typing.Dict[str, typing.List[int]]
    ] = collections.defaultdict(dict)
    async for queue in redis_cache.scan_iter(
        match=f"merge-*~{owner_id}~*", count=10000
    ):
        queue_type, _, repo_id, branch = queue.split("~")
        if queue_type == "merge-queue":
            queues[repo_id][branch] = [
                int(pull) async for pull, _ in redis_cache.zscan_iter(queue)
            ]
        elif queue_type == "merge-train":
            train_raw = await redis_cache.get(queue)
            train = typing.cast(merge_train.Train.Serialized, json.loads(train_raw))
            _, _, repo_id, branch = queue.split("~")
            queues[repo_id][branch] = [
                int(c["user_pull_request_number"]) for c in train["cars"]
            ] + [int(wp[0]) for wp in train["waiting_pulls"]]

    return responses.JSONResponse(status_code=200, content=queues)


@app.post("/event", dependencies=[fastapi.Depends(auth.signature)])
async def event_handler(
    request: requests.Request,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
    redis_stream: utils.RedisStream = fastapi.Depends(  # noqa: B008
        redis.get_redis_stream
    ),
) -> responses.Response:
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    try:
        await github_events.filter_and_dispatch(
            redis_cache, redis_stream, event_type, event_id, data
        )
    except github_events.IgnoredEvent as ie:
        status_code = 200
        reason = f"Event ignored: {ie.reason}"
    else:
        status_code = 202
        reason = "Event queued"

    if (
        config.WEBHOOK_APP_FORWARD_URL
        and config.WEBHOOK_FORWARD_EVENT_TYPES is not None
        and event_type in config.WEBHOOK_FORWARD_EVENT_TYPES
    ):
        raw = await request.body()
        try:
            async with http.AsyncClient(timeout=EVENT_FORWARD_TIMEOUT) as client:
                await client.post(
                    config.WEBHOOK_APP_FORWARD_URL,
                    content=raw.decode(),
                    headers={
                        "X-GitHub-Event": event_type,
                        "X-GitHub-Delivery": event_id,
                        "X-Hub-Signature": request.headers.get("X-Hub-Signature"),
                        "User-Agent": request.headers.get("User-Agent"),
                        "Content-Type": request.headers.get("Content-Type"),
                    },
                )
        except httpx.TimeoutException:
            LOG.warning(
                "Fail to forward GitHub event",
                event_type=event_type,
                event_id=event_id,
                sender=data["sender"]["login"],
            )

    return responses.Response(reason, status_code=status_code)


@app.get("/")
async def index(
    setup_action: typing.Optional[str] = None,
) -> responses.Response:  # pragma: no cover
    if setup_action:
        return responses.Response(
            "Your Mergify installation succeeded.",
            status_code=200,
        )
    return responses.RedirectResponse(url="https://mergify.io/")
