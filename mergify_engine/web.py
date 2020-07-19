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


import asyncio
import collections
import functools
import hmac
import json
from urllib.parse import urlsplit
import uuid

import daiquiri
import fastapi
from starlette import requests
from starlette import responses
from starlette.middleware import cors
import voluptuous

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import github_events
from mergify_engine import rules
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner


LOG = daiquiri.getLogger(__name__)

app = fastapi.FastAPI()


@app.on_event("startup")
async def startup():
    app.aredis_stream = await utils.create_aredis_for_stream()


@app.on_event("shutdown")
async def shutdown():
    app.aredis_stream.connection_pool.disconnect()


async def authentification(request: requests.Request):
    # Only SHA1 is supported
    header_signature = request.headers.get("X-Hub-Signature")
    if header_signature is None:
        LOG.warning("Webhook without signature")
        raise fastapi.HTTPException(status_code=403)

    try:
        sha_name, signature = header_signature.split("=")
    except ValueError:
        sha_name = None

    if sha_name != "sha1":
        LOG.warning("Webhook signature malformed")
        raise fastapi.HTTPException(status_code=403)

    body = await request.body()
    mac = utils.compute_hmac(body)
    if not hmac.compare_digest(mac, str(signature)):
        LOG.warning("Webhook signature invalid")
        raise fastapi.HTTPException(status_code=403)


async def simulator_authentification(request: requests.Request):
    authorization = request.headers.get("Authorization")
    if authorization:
        if authorization.startswith("token "):
            try:
                options = http.DEFAULT_CLIENT_OPTIONS.copy()
                options["headers"]["Authorization"] = authorization
                async with http.AsyncClient(
                    base_url=config.GITHUB_API_URL, **options
                ) as client:
                    await client.get("/user")
            except http.HTTPError as e:
                raise fastapi.HTTPException(status_code=e.response.status_code)
        else:
            raise fastapi.HTTPException(status_code=403)
    else:
        await authentification(request)


async def http_post(*args, **kwargs):
    # Set the maximum timeout to 3 seconds: GitHub is not going to wait for
    # more than 10 seconds for us to accept an event, so if we're unable to
    # forward an event in 3 seconds, just drop it.
    async with http.AsyncClient(timeout=5) as client:
        await client.post(*args, **kwargs)


def _get_badge_url(owner, repo, ext, style):
    return responses.RedirectResponse(
        url=f"https://img.shields.io/endpoint.{ext}?url=https://dashboard.mergify.io/badges/{owner}/{repo}&style={style}",
        status_code=302,
    )


@app.get("/badges/{owner}/{repo}.png")
async def badge_png(owner, repo, style: str = "flat"):  # pragma: no cover
    return _get_badge_url(owner, repo, "png", style)


@app.get("/badges/{owner}/{repo}.svg")
async def badge_svg(owner, repo, style: str = "flat"):  # pragma: no cover
    return _get_badge_url(owner, repo, "svg", style)


@app.get("/badges/{owner}/{repo}")
async def badge(owner, repo):
    return responses.RedirectResponse(
        url=f"https://dashboard.mergify.io/badges/{owner}/{repo}"
    )


config_validator_app = fastapi.FastAPI()
config_validator_app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@config_validator_app.post("/")
async def config_validator(
    data: fastapi.UploadFile = fastapi.File(...),
):  # pragma: no cover
    try:
        rules.UserConfigurationSchema(await data.read())
    except Exception as e:
        status = 400
        message = str(e)
    else:
        status = 200
        message = "The configuration is valid"

    return responses.PlainTextResponse(message, status_code=status)


app.mount("/validate", config_validator_app)


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
        app.aredis_stream, event_type, str(uuid.uuid4()), data
    )

    return responses.Response("Refresh queued", status_code=202)


@app.post("/refresh/{owner}/{repo}", dependencies=[fastapi.Depends(authentification)])
async def refresh_repo(owner, repo):
    return await _refresh(owner, repo)


RefreshActionSchema = voluptuous.Schema(voluptuous.Any("user", "forced"))


@app.post(
    "/refresh/{owner}/{repo}/pull/{pull}",
    dependencies=[fastapi.Depends(authentification)],
)
async def refresh_pull(owner, repo, pull: int, action="user"):
    action = RefreshActionSchema(action)
    return await _refresh(owner, repo, action=action, pull_request={"number": pull})


@app.post(
    "/refresh/{owner}/{repo}/branch/{branch}",
    dependencies=[fastapi.Depends(authentification)],
)
async def refresh_branch(owner, repo, branch):
    return await _refresh(owner, repo, ref=f"refs/heads/{branch}")


@app.put(
    "/subscription-cache/{owner_id}", dependencies=[fastapi.Depends(authentification)],
)
async def subscription_cache_update(
    owner_id, request: requests.Request
):  # pragma: no cover
    sub = await request.json()
    if sub is None:
        return responses.Response("Empty content", status_code=400)
    await sub_utils.save_subscription_to_cache(owner_id, sub)
    return responses.Response("Cache updated", status_code=200)


@app.delete(
    "/subscription-cache/{owner_id}", dependencies=[fastapi.Depends(authentification)],
)
async def subscription_cache_delete(owner_id):  # pragma: no cover
    r = await utils.get_aredis_for_cache()
    await r.delete("subscription-cache-owner-%s" % owner_id)
    return responses.Response("Cache cleaned", status_code=200)


class PullRequestUrlInvalid(voluptuous.Invalid):
    pass


@voluptuous.message("expected a Pull Request URL", cls=PullRequestUrlInvalid)
def PullRequestUrl(v):
    _, owner, repo, _, pull_number = urlsplit(v).path.split("/")
    pull_number = int(pull_number)
    return owner, repo, pull_number


SimulatorSchema = voluptuous.Schema(
    {
        voluptuous.Required("pull_request"): voluptuous.Any(None, PullRequestUrl()),
        voluptuous.Required("mergify.yml"): rules.UserConfigurationSchema,
    }
)


def ensure_no_voluptuous(value):
    if isinstance(value, (dict, list, str)):
        return value
    else:
        return str(value)


def voluptuous_error(error):
    return {
        "type": error.__class__.__name__,
        "message": error.error_message,
        "error": str(error),
        "details": list(map(ensure_no_voluptuous, error.path)),
    }


@app.exception_handler(voluptuous.Invalid)
async def voluptuous_errors(request: requests.Request, exc: voluptuous.Invalid):
    # FIXME(sileht): remove error at payload root
    payload = voluptuous_error(exc)
    payload["errors"] = []
    if isinstance(exc, voluptuous.MultipleInvalid):
        payload["errors"].extend(map(voluptuous_error, sorted(exc.errors, key=str)))
    else:
        payload["errors"].extend(voluptuous_error(exc))
    return responses.JSONResponse(status_code=400, content=payload)


def _sync_simulator(pull_request_rules, owner, repo, pull_number, token):
    try:
        if token:
            auth = github.GithubTokenAuth(owner, repo, token)
        else:
            auth = github.get_auth(owner, repo)

        with github.get_client(auth=auth) as client:
            try:
                data = client.item(f"pulls/{pull_number}")
            except http.HTTPNotFound:
                raise PullRequestUrlInvalid(
                    message=f"Pull request '{owner}/{repo}/pull/{pull_number}' not found"
                )

            subscription = asyncio.run(sub_utils.get_subscription(client.auth.owner_id))

            ctxt = context.Context(
                client,
                data,
                subscription,
                [{"event_type": "mergify-simulator", "data": []}],
            )
            match = pull_request_rules.get_pull_request_rule(ctxt)
            return actions_runner.gen_summary(ctxt, match)
    except exceptions.MergifyNotInstalled:
        raise PullRequestUrlInvalid(
            message=f"Mergify not installed on repository '{owner}/{repo}'"
        )


@app.post("/simulator", dependencies=[fastapi.Depends(simulator_authentification)])
async def simulator(request: requests.Request):
    token = request.headers.get("Authorization")
    if token:
        token = token[6:]  # Drop 'token '

    data = SimulatorSchema(await request.json())
    if data["pull_request"]:
        loop = asyncio.get_running_loop()
        title, summary = await loop.run_in_executor(
            None,
            functools.partial(
                _sync_simulator,
                data["mergify.yml"]["pull_request_rules"],
                *data["pull_request"],
                token=token,
            ),
        )
    else:
        title, summary = ("The configuration is valid", None)

    return responses.JSONResponse(
        status_code=200, content={"title": title, "summary": summary}
    )


async def cleanup_subscription(data):
    try:
        installation = await github_app.get_installation(
            data["marketplace_purchase"]["account"]
        )
    except exceptions.MergifyNotInstalled:
        return

    redis = await utils.get_aredis_for_cache()
    await redis.delete("subscription-cache-%s" % installation["id"])


@app.post("/marketplace", dependencies=[fastapi.Depends(authentification)])
async def marketplace_handler(request: requests.Request,):  # pragma: no cover
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


@app.get("/queues/{installation_id}", dependencies=[fastapi.Depends(authentification)])
async def queues(installation_id):
    redis = await utils.get_aredis_for_cache()
    queues = collections.defaultdict(dict)
    async for queue in redis.scan_iter(
        match=f"strict-merge-queues~{installation_id}~*"
    ):
        _, _, owner, repo, branch = queue.split("~")
        queues[owner + "/" + repo][branch] = [
            int(pull) async for pull, _ in redis.zscan_iter(queue)
        ]

    return responses.JSONResponse(status_code=200, content=queues)


@app.post("/event", dependencies=[fastapi.Depends(authentification)])
async def event_handler(request: requests.Request,):
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()

    try:
        await github_events.job_filter_and_dispatch(
            app.aredis_stream, event_type, event_id, data
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
@app.delete("/events-testing", dependencies=[fastapi.Depends(authentification)])
async def event_testing_handler_delete():  # pragma: no cover
    r = await utils.get_aredis_for_cache()
    await r.delete("events-testing")
    return responses.Response("Event queued", status_code=202)


@app.post("/events-testing", dependencies=[fastapi.Depends(authentification)])
async def event_testing_handler_post(request: requests.Request):  # pragma: no cover
    r = await utils.get_aredis_for_cache()
    event_type = request.headers.get("X-GitHub-Event")
    event_id = request.headers.get("X-GitHub-Delivery")
    data = await request.json()
    await r.rpush(
        "events-testing",
        json.dumps({"id": event_id, "type": event_type, "payload": data}),
    )
    return responses.Response("Event queued", status_code=202)


@app.get("/events-testing", dependencies=[fastapi.Depends(authentification)])
async def event_testing_handler_get(number: int = None):  # pragma: no cover
    r = await utils.get_aredis_for_cache()
    async with await r.pipeline() as p:
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
