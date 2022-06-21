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
import httpx
from starlette import requests
from starlette import responses

from mergify_engine import config
from mergify_engine import github_events
from mergify_engine import redis_utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.web import auth
from mergify_engine.web import redis


LOG = daiquiri.getLogger(__name__)

# Set the maximum timeout to 5 seconds: GitHub is not going to wait for
# more than 10 seconds for us to accept an event, so if we're unable to
# forward an event in 5 seconds, just drop it.
EVENT_FORWARD_TIMEOUT = 5


router = fastapi.APIRouter()


@router.post("/marketplace", dependencies=[fastapi.Depends(auth.signature)])
async def marketplace_handler(
    request: requests.Request,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
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
        redis_links.cache, data["marketplace_purchase"]["account"]["id"]
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
        except httpx.HTTPError:
            LOG.warning(
                "Fail to forward Marketplace event",
                event_type=event_type,
                event_id=event_id,
                sender=data["sender"]["login"],
                gh_owner=data["marketplace_purchase"]["account"]["login"],
            )

    return responses.Response("Event queued", status_code=202)


@router.post("/event", dependencies=[fastapi.Depends(auth.signature)])
async def event_handler(
    request: requests.Request,
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> responses.Response:
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    try:
        await github_events.filter_and_dispatch(redis_links, event_type, event_id, data)
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
        except httpx.HTTPError:
            LOG.warning(
                "Fail to forward GitHub event",
                event_type=event_type,
                event_id=event_id,
                sender=data["sender"]["login"],
            )

    return responses.Response(reason, status_code=status_code)
