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

import typing

import daiquiri
import fastapi

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.dashboard import application as application_mod
from mergify_engine.web import redis


LOG = daiquiri.getLogger(__name__)

ScopeT = typing.NewType("ScopeT", str)

security = fastapi.security.http.HTTPBearer()


async def get_application(
    request: fastapi.Request,
    credentials: fastapi.security.HTTPAuthorizationCredentials = fastapi.Security(  # noqa: B008
        security
    ),
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> application_mod.Application:

    scope: typing.Optional[github_types.GitHubLogin] = request.path_params.get("owner")
    api_access_key = credentials.credentials[: config.API_ACCESS_KEY_LEN]
    api_secret_key = credentials.credentials[config.API_ACCESS_KEY_LEN :]
    try:
        app = await application_mod.Application.get(
            redis_cache, api_access_key, api_secret_key, scope
        )
    except application_mod.ApplicationUserNotFound:
        raise fastapi.HTTPException(status_code=403)

    # Seatbelt
    current_scope = None if app.account_scope is None else app.account_scope["login"]
    if scope is not None and current_scope != scope:
        LOG.error(
            "got application with wrong scope",
            expected_scope=scope,
            current_scope=current_scope,
        )
        raise fastapi.HTTPException(status_code=403)
    return app


# Just an alias to help readability of fastapi.Depends
require_authentication = get_application


async def get_installation(
    application: application_mod.Application = fastapi.Depends(  # noqa: B008
        get_application
    ),
) -> github_types.GitHubInstallation:
    if application.account_scope is None:
        raise fastapi.HTTPException(status_code=403)

    try:
        return await github.get_installation_from_account_id(
            application.account_scope["id"]
        )
    except exceptions.MergifyNotInstalled:
        raise fastapi.HTTPException(status_code=403)
