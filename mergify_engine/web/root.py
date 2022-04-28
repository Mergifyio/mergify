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
import fastapi
from starlette import responses
from starlette.middleware import cors

from mergify_engine.web import dashboard
from mergify_engine.web import github
from mergify_engine.web import legacy_badges
from mergify_engine.web import redis
from mergify_engine.web import refresher
from mergify_engine.web import utils
from mergify_engine.web.api import root as api_root


LOG = daiquiri.getLogger(__name__)

app = fastapi.FastAPI(openapi_url=None, redoc_url=None, docs_url=None)
# NOTE(sileht): We don't set any origins on purpose as the API is public and all customers
# should be able to access the API with their applications from a browser or anything else.
app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard.router)
app.include_router(github.router)
app.include_router(refresher.router)
app.include_router(legacy_badges.router, prefix="/badges")

app.mount("/v1", api_root.app)

utils.setup_exception_handlers(app)


@app.on_event("startup")
async def startup() -> None:
    await redis.startup()


@app.on_event("shutdown")
async def shutdown() -> None:
    await redis.shutdown()


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
