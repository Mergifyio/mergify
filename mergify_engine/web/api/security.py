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

import fastapi

from mergify_engine import config
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.dashboard import application as application_mod
from mergify_engine.web import redis


security = fastapi.security.http.HTTPBearer()


async def get_installation(
    credentials: fastapi.security.HTTPAuthorizationCredentials = fastapi.Security(  # noqa: B008
        security
    ),
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> github_types.GitHubInstallation:
    api_access_key = credentials.credentials[: config.API_ACCESS_KEY_LEN]
    api_secret_key = credentials.credentials[config.API_ACCESS_KEY_LEN :]
    try:
        application = await application_mod.Application.get(
            redis_cache, api_access_key, api_secret_key
        )
    except application_mod.ApplicationUserNotFound:
        raise fastapi.HTTPException(status_code=403)

    return await github.get_installation_from_account_id(application.account_id)
