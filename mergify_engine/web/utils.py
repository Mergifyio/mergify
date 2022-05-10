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
from datadog import statsd
import fastapi
from redis import exceptions as redis_exceptions
from starlette import requests
from starlette import responses

from mergify_engine import exceptions as engine_exceptions


LOG = daiquiri.getLogger(__name__)


def setup_exception_handlers(app: fastapi.FastAPI) -> None:
    @app.exception_handler(redis_exceptions.ConnectionError)
    async def redis_errors(
        request: requests.Request, exc: redis_exceptions.ConnectionError
    ) -> responses.Response:
        statsd.increment("redis.client.connection.errors")
        LOG.warning("FastAPI lost Redis connection", exc_info=exc)
        return responses.Response(status_code=503)

    @app.exception_handler(engine_exceptions.RateLimited)
    async def rate_limited_handler(
        request: requests.Request, exc: engine_exceptions.RateLimited
    ) -> responses.JSONResponse:
        return responses.JSONResponse(
            status_code=403,
            content={"message": "Organization or user has hit GitHub API rate limit"},
        )
