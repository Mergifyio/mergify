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
import typing

import daiquiri
from datadog import statsd
import fastapi
from starlette import requests
from starlette import responses
import yaaredis

from mergify_engine.web import badges
from mergify_engine.web import config_validator
from mergify_engine.web import dashboard
from mergify_engine.web import github
from mergify_engine.web import legacy_queue
from mergify_engine.web import redis
from mergify_engine.web import refresher
from mergify_engine.web import simulator
from mergify_engine.web.api import root as api_root


LOG = daiquiri.getLogger(__name__)

app = fastapi.FastAPI()
app.include_router(dashboard.router)
app.include_router(github.router)
app.include_router(refresher.router)
app.include_router(legacy_queue.router)

app.mount("/simulator", simulator.app)
app.mount("/validate", config_validator.app)
app.mount("/badges", badges.app)
app.mount("/v1", api_root.app)


@app.on_event("startup")
async def startup() -> None:
    await redis.startup()


@app.on_event("shutdown")
async def shutdown() -> None:
    await redis.shutdown()


@app.exception_handler(yaaredis.exceptions.ConnectionError)
async def redis_errors(
    request: requests.Request, exc: yaaredis.exceptions.ConnectionError
) -> responses.JSONResponse:
    statsd.increment("redis.client.connection.errors")
    LOG.warning("FastAPI lost Redis connection", exc_info=exc)
    return responses.JSONResponse(status_code=503)


@app.get("/")
async def index(
    setup_action: typing.Optional[str] = None,
) -> responses.Response:  # pragma: no cover
    if setup_action:
        return responses.Response(
            "Your Mergify installation succeeded.",
            status_code=200,
        )
    return responses.RedirectResponse(url="https://mergify.com")
