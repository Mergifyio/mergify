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

import fastapi

from mergify_engine.clients import github
from mergify_engine.clients import http


security = fastapi.security.http.HTTPBearer()


async def get_github_client(
    credentials: fastapi.security.HTTPAuthorizationCredentials = fastapi.Security(  # noqa: B008
        security
    ),
) -> typing.AsyncGenerator[github.AsyncGithubInstallationClient, None]:
    # TODO(sileht): convert it on dashboard
    # TODO(sileht): cache dashboard calls
    token = credentials.credentials

    auth = github.GithubTokenAuth(token)
    async with github.aget_client(auth=auth) as client:
        try:
            await client.item("/user")
        except (http.HTTPForbidden, http.HTTPUnauthorized):
            raise fastapi.HTTPException(status_code=403)

        yield client
