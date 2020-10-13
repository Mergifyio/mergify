# -*- encoding: utf-8 -*-
#
# Copyright © 2019–2020 Mergify SAS
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
import json
import uuid

import aredis
import daiquiri
import fastapi
from starlette import requests
from starlette import responses
import voluptuous

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github_app
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


_AREDIS_STREAM: aredis.StrictRedis = None
_AREDIS_CACHE: aredis.StrictRedis = None


@app.on_event("startup")
async def startup():
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_STREAM = await utils.create_aredis_for_stream()
    _AREDIS_CACHE = await utils.get_aredis_for_cache()


@app.on_event("shutdown")
async def shutdown():
    global _AREDIS_STREAM, _AREDIS_CACHE
    _AREDIS_CACHE.connection_pool.max_idle_time = 0
    _AREDIS_CACHE.connection_pool.disconnect()
    _AREDIS_STREAM.connection_pool.max_idle_time = 0
    _AREDIS_STREAM.connection_pool.disconnect()
    _AREDIS_CACHE = None
    _AREDIS_STREAM = None
    await utils.stop_pending_aredis_tasks()


async def http_post(*args, **kwargs):
    # Set the maximum timeout to 3 seconds: GitHub is not going to wait for
    # more than 10 seconds for us to accept an event, so if we're unable to
    # forward an event in 3 seconds, just drop it.
    async with http.AsyncClient(timeout=5) as client:
        await client.post(*args, **kwargs)


async def _refresh(owner, repo, action="user", **extra_data):
    event_type = "refresh"
    data = {
        "action": action,
        "repository": {
            "name": repo,
            "owner": {"login": owner},
            "full_name": f"{owner}/{repo}",
        },
        "sender": {"login": "<internal>"},
    }
    data.update(extra_data)

    await github_events.job_filter_and_dispatch(
        _AREDIS_STREAM, event_type, str(uuid.uuid4()), data
    )

    return responses.Response("Refresh queued", status_code=202)


@app.post("/refresh/{owner}/{repo}", dependencies=[fastapi.Depends(auth.signature)])
async def refresh_repo(owner, repo):
    return await _refresh(owner, repo)


RefreshActionSchema = voluptuous.Schema(voluptuous.Any("user", "forced"))


@app.post(
    "/refresh/{owner}/{repo}/pull/{pull}",
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_pull(owner, repo, pull: int, action="user"):
    action = RefreshActionSchema(action)
    return await _refresh(owner, repo, action=action, pull_request={"number": pull})


@app.post(
    "/refresh/{owner}/{repo}/branch/{branch}",
    dependencies=[fastapi.Depends(auth.signature)],
)
async def refresh_branch(owner, repo, branch):
    return await _refresh(owner, repo, ref=f"refs/heads/{branch}")


@app.put(
    "/subscription-cache/{owner_id}",
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_update(
    owner_id, request: requests.Request
):  # pragma: no cover
    sub = await request.json()
    if sub is None:
        return responses.Response("Empty content", status_code=400)
    await subscription.Subscription.from_dict(
        owner_id, sub
    ).save_subscription_to_cache()
    return responses.Response("Cache updated", status_code=200)


@app.delete(
    "/subscription-cache/{owner_id}",
    dependencies=[fastapi.Depends(auth.signature)],
)
async def subscription_cache_delete(owner_id):  # pragma: no cover
    await _AREDIS_CACHE.delete("subscription-cache-owner-%s" % owner_id)
    return responses.Response("Cache cleaned", status_code=200)


async def cleanup_subscription(data):
    try:
        installation = await github_app.get_installation(
            data["marketplace_purchase"]["account"]
        )
    except exceptions.MergifyNotInstalled:
        return

    await _AREDIS_CACHE.delete("subscription-cache-%s" % installation["id"])


@app.post("/marketplace", dependencies=[fastapi.Depends(auth.signature)])
async def marketplace_handler(
    request: requests.Request,
):  # pragma: no cover
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

    await cleanup_subscription(data)

    if config.WEBHOOK_MARKETPLACE_FORWARD_URL:
        raw = await request.body()
        await http_post(
            config.WEBHOOK_MARKETPLACE_FORWARD_URL,
            data=raw.decode(),
            headers={
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": event_id,
                "X-Hub-Signature": request.headers.get("X-Hub-Signature"),
                "User-Agent": request.headers.get("User-Agent"),
                "Content-Type": request.headers.get("Content-Type"),
            },
        )

    return responses.Response("Event queued", status_code=202)


@app.get("/queues/{installation_id}", dependencies=[fastapi.Depends(auth.signature)])
async def queues(installation_id):
    queues = collections.defaultdict(dict)
    async for queue in _AREDIS_CACHE.scan_iter(
        match=f"strict-merge-queues~{installation_id}~*"
    ):
        _, _, owner, repo, branch = queue.split("~")
        queues[owner + "/" + repo][branch] = [
            int(pull) async for pull, _ in _AREDIS_CACHE.zscan_iter(queue)
        ]

    return responses.JSONResponse(status_code=200, content=queues)


@app.post("/event", dependencies=[fastapi.Depends(auth.signature)])
async def event_handler(
    request: requests.Request,
):
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    try:
        await github_events.job_filter_and_dispatch(
            _AREDIS_STREAM, event_type, event_id, data
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
        await http_post(
            config.WEBHOOK_APP_FORWARD_URL,
            data=raw.decode(),
            headers={
                "X-GitHub-Event": event_type,
                "X-GitHub-Delivery": event_id,
                "X-Hub-Signature": request.headers.get("X-Hub-Signature"),
                "User-Agent": request.headers.get("User-Agent"),
                "Content-Type": request.headers.get("Content-Type"),
            },
        )

    return responses.Response(reason, status_code=status_code)


# NOTE(sileht): These endpoints are used for recording cassetes, we receive
# Github event on POST, we store them is redis, GET to retreive and delete
@app.delete("/events-testing", dependencies=[fastapi.Depends(auth.signature)])
async def event_testing_handler_delete():  # pragma: no cover
    await _AREDIS_CACHE.delete("events-testing")
    return responses.Response("Event queued", status_code=202)


@app.post("/events-testing", dependencies=[fastapi.Depends(auth.signature)])
async def event_testing_handler_post(request: requests.Request):  # pragma: no cover
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()
    await _AREDIS_CACHE.rpush(
        "events-testing",
        json.dumps({"id": event_id, "type": event_type, "payload": data}),
    )
    return responses.Response("Event queued", status_code=202)


@app.get("/events-testing", dependencies=[fastapi.Depends(auth.signature)])
async def event_testing_handler_get(number: int = None):  # pragma: no cover
    async with await _AREDIS_CACHE.pipeline() as p:
        if number is None:
            await p.lrange("events-testing", 0, -1)
            await p.delete("events-testing")
            values = (await p.execute())[0]
        else:
            for _ in range(number):
                await p.lpop("events-testing")
            values = await p.execute()
    data = [json.loads(i) for i in values if i is not None]
    return responses.JSONResponse(content=data)


@app.post("/marketplace-testing")
async def marketplace_testng_handler(request: requests.Request):  # pragma: no cover
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()
    LOG.debug(
        "received marketplace testing events",
        event_type=event_type,
        event_id=event_id,
        data=data,
    )
    return responses.Response("Event ignored", status_code=202)


@app.get("/")
async def index():  # pragma: no cover
    return responses.RedirectResponse(url="https://mergify.io/")
