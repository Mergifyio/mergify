# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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


import hmac

import daiquiri
import fastapi
from starlette import requests

from mergify_engine import config
from mergify_engine import utils
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)


async def signature(request: requests.Request):
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


async def token(request: requests.Request):
    authorization = request.headers.get("Authorization")
    if authorization:
        if authorization.startswith("token "):
            try:
                options = http.DEFAULT_CLIENT_OPTIONS.copy()
                options["headers"]["Authorization"] = authorization  # type: ignore
                async with http.AsyncClient(
                    base_url=config.GITHUB_API_URL, **options  # type: ignore
                ) as client:
                    await client.get("/user")
                    return
            except http.HTTPStatusError as e:
                raise fastapi.HTTPException(status_code=e.response.status_code)

    raise fastapi.HTTPException(status_code=403)


async def signature_or_token(request: requests.Request):
    authorization = request.headers.get("Authorization")
    if authorization:
        await token(request)
    else:
        await signature(request)
