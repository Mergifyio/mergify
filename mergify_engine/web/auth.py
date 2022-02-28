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


LOG = daiquiri.getLogger(__name__)


async def signature(request: requests.Request) -> None:
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

    current_hmac = utils.compute_hmac(body, config.WEBHOOK_SECRET)
    if hmac.compare_digest(current_hmac, str(signature)):
        return

    if config.WEBHOOK_SECRET_PRE_ROTATION is not None:
        future_hmac = utils.compute_hmac(body, config.WEBHOOK_SECRET_PRE_ROTATION)
        if hmac.compare_digest(future_hmac, str(signature)):
            return

    LOG.warning("Webhook signature invalid")
    raise fastapi.HTTPException(status_code=403)


async def dashboard(request: requests.Request) -> None:
    authorization = request.headers.get("Authorization")
    if authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:]
            if token == config.DASHBOARD_TO_ENGINE_API_KEY:
                return

            if (
                config.DASHBOARD_TO_ENGINE_API_KEY_PRE_ROTATION is not None
                and token == config.DASHBOARD_TO_ENGINE_API_KEY_PRE_ROTATION
            ):
                return

    # fallback to legacy signature authorization
    await signature(request)
