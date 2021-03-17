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

import daiquiri
import fastapi
import httpx
from starlette import requests
from starlette import responses
import voluptuous

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.web import auth
from mergify_engine.web import badges
from mergify_engine.web import config_validator
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

_AREDIS_STREAM: utils.RedisStream
_AREDIS_CACHE: utils.RedisCache


@app.on_event("startup")
async def startup() -> None:
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_STREAM = await utils.create_aredis_for_stream(
        max_connections=config.REDIS_STREAM_WEB_MAX_CONNECTIONS
    )
    _AREDIS_CACHE = await utils.create_aredis_for_cache(
        max_connections=config.REDIS_CACHE_WEB_MAX_CONNECTIONS
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    LOG.info("asgi: starting redis shutdown")
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_CACHE.connection_pool.max_idle_time = 0
    _AREDIS_CACHE.connection_pool.disconnect()
    _AREDIS_STREAM.connection_pool.max_idle_time = 0
    _AREDIS_STREAM.connection_pool.disconnect()
    LOG.info("asgi: waiting redis pending tasks to complete")
    await utils.stop_pending_aredis_tasks()
    LOG.info("asgi: finished redis shutdown")


@app.get("/installation")  # noqa: FS003
async def installation() -> responses.Response:
    return responses.Response(
        "Your mergify installation succeed, the installer have been disabled.",
        status_code=200,
    )


@app.post(
    "/refresh/{owner}/{repo_name}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_repo(
    owner: github_types.GitHubLogin, repo_name: github_types.GitHubRepositoryName
) -> responses.Response:
    global _AREDIS_STREAM, _AREDIS_CACHE
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    await github_events.send_refresh(_AREDIS_CACHE, _AREDIS_STREAM, repository)
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
) -> responses.Response:
    action = RefreshActionSchema(action)
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    global _AREDIS_STREAM, _AREDIS_CACHE
    await github_events.send_refresh(
        _AREDIS_CACHE,
        _AREDIS_STREAM,
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
) -> responses.Response:
    async with github.aget_client(owner_name=owner) as client:
        try:
            repository = await client.item(f"/repos/{owner}/{repo_name}")
        except http.HTTPNotFound:
            return responses.JSONResponse(
                status_code=404, content="repository not found"
            )

    global _AREDIS_STREAM, _AREDIS_CACHE
    await github_events.send_refresh(
        _AREDIS_CACHE,
        _AREDIS_STREAM,
        repository,
        ref=github_types.GitHubRefType(f"refs/heads/{branch}"),
    )
    return responses.Response("Refresh queued", status_code=202)


@app.put(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_update(
    owner_id: str, request: requests.Request
) -> responses.Response:  # pragma: no cover
    sub = await request.json()
    if sub is None:
        return responses.Response("Empty content", status_code=400)
    global _AREDIS_CACHE
    await subscription.Subscription.from_dict(
        _AREDIS_CACHE, int(owner_id), sub
    ).save_subscription_to_cache()
    return responses.Response("Cache updated", status_code=200)


@app.delete(
    "/subscription-cache/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_delete(owner_id):  # pragma: no cover
    global _AREDIS_CACHE
    await subscription.Subscription.delete(_AREDIS_CACHE, owner_id)
    return responses.Response("Cache cleaned", status_code=200)


@app.post("/marketplace", dependencies=[fastapi.Depends(auth.signature)])
async def marketplace_handler(
    request: requests.Request,
) -> responses.Response:  # pragma: no cover
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

    global _AREDIS_CACHE
    await subscription.Subscription.delete(
        _AREDIS_CACHE, data["marketplace_purchase"]["account"]["id"]
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
async def queues_by_owner_id(owner_id):
    global _AREDIS_CACHE
    queues = collections.defaultdict(dict)
    async for queue in _AREDIS_CACHE.scan_iter(match=f"merge-queue~{owner_id}~*"):
        _, _, repo_id, branch = queue.split("~")
        async with github.aget_client(owner_id=owner_id) as client:
            try:
                repo = await client.item(f"/repositories/{repo_id}")
            except exceptions.RateLimited:
                return responses.JSONResponse(
                    status_code=403,
                    content={
                        "message": f"{client.auth.owner} account with {client.auth.owner_id} ID, rate limited by GitHub"
                    },
                )
            queues[client.auth.owner + "/" + repo["name"]][branch] = [
                int(pull) async for pull, _ in _AREDIS_CACHE.zscan_iter(queue)
            ]

    return responses.JSONResponse(status_code=200, content=queues)


@app.post("/event", dependencies=[fastapi.Depends(auth.signature)])
async def event_handler(
    request: requests.Request,
) -> responses.Response:
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    global _AREDIS_STREAM, _AREDIS_CACHE
    try:
        await github_events.filter_and_dispatch(
            _AREDIS_CACHE, _AREDIS_STREAM, event_type, event_id, data
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
async def index():  # pragma: no cover
    return responses.RedirectResponse(url="https://mergify.io/")
