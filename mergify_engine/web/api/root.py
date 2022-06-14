# -*- encoding: utf-8 -*-
# flake8: noqa: B008
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
import argparse
import collections
import dataclasses
import datetime
import json
import os
import typing

import fastapi
import pydantic
from starlette import requests
from starlette import responses
from starlette.middleware import cors

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions as engine_exceptions
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.queue import merge_train
from mergify_engine.web import api
from mergify_engine.web import redis
from mergify_engine.web import utils as web_utils
from mergify_engine.web.api import applications
from mergify_engine.web.api import badges
from mergify_engine.web.api import eventlogs
from mergify_engine.web.api import queues
from mergify_engine.web.api import security
from mergify_engine.web.api import simulator


def api_enabled() -> None:
    if not config.API_ENABLE:
        raise fastapi.HTTPException(status_code=404)


app = fastapi.FastAPI(
    title="Mergify API",
    description="Faster & safer code merge",
    version="v1",
    terms_of_service="https://mergify.com/tos",
    contact={
        "name": "Mergify",
        "url": "https://mergify.com",
        "email": "support@mergify.com",
    },
    openapi_url=None,
    redoc_url=None,
    docs_url=None,
    openapi_tags=[
        {
            "name": "applications",
            "description": "Operations with applications.",
        },
        {
            "name": "badges",
            "description": "Operations with badges.",
        },
        {
            "name": "eventlogs",
            "description": "Operations with event logs.",
        },
        {
            "name": "queues",
            "description": "Operations with queues.",
        },
        {
            "name": "simulator",
            "description": "Mergify configuration simulator",
        },
    ],
    servers=[{"url": "https://api.mergify.com/v1", "description": "default"}],
    dependencies=[fastapi.Depends(api_enabled)],
    reponses=api.default_responses,
)
app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(applications.router)
app.include_router(queues.router)
app.include_router(badges.router)
app.include_router(simulator.router)
app.include_router(eventlogs.router)

web_utils.setup_exception_handlers(app)


def generate_openapi_spec() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenAPI spec file")
    parser.add_argument("output", help="output file")
    args = parser.parse_args()

    path = os.path.dirname(args.output)
    if path:
        os.makedirs(path, exist_ok=True)

    with open(args.output, "w") as f:
        json.dump(fp=f, obj=app.openapi())
