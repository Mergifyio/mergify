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

import threading
import time

import daiquiri
import httpx
import jwt

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

EXPECTED_MINIMAL_PERMISSIONS = {
    "Organization": {
        "checks": "write",
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pages": "write",
        "pull_requests": "write",
        "statuses": "read",
        "members": "read",
    },
    "User": {
        "checks": "write",
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pages": "write",
        "pull_requests": "write",
        "statuses": "read",
    },
}


class JwtHandler:
    JWT_EXPIRATION = 60

    def __init__(self):
        self.jwt = None
        self.jwt_expiration = None
        self.lock = threading.Lock()

    def get_or_create(self, force=False):
        now = int(time.time())
        with self.lock:
            if force or self.jwt is None or self.jwt_expiration <= now:
                self.jwt_expiration = now + self.JWT_EXPIRATION
                payload = {
                    "iat": now,
                    "exp": self.jwt_expiration,
                    "iss": config.INTEGRATION_ID,
                }
                encrypted = jwt.encode(
                    payload, key=config.PRIVATE_KEY, algorithm="RS256"
                )
                self.jwt = encrypted.decode("utf-8")
                LOG.info("New JWT created", expire_at=self.jwt_expiration)
        return self.jwt


get_or_create_jwt = JwtHandler().get_or_create


def permissions_need_to_be_updated(installation):
    expected_permissions = EXPECTED_MINIMAL_PERMISSIONS[installation["target_type"]]
    for perm_name, perm_level in expected_permissions.items():
        if installation["permissions"].get(perm_name) != perm_level:
            LOG.debug(
                "The Mergify installation doesn't have the required permissions",
                gh_owner=installation["account"]["login"],
                permissions=installation["permissions"],
            )
            # FIXME(sileht): Looks like ton of people have not all permissions
            # Or this is buggy, so disable it for now.
            if perm_name in ["checks", "pull_requests", "contents"]:
                raise exceptions.MergifyNotInstalled()
            return True
    return False


class GithubBearerAuth(httpx.Auth):
    def auth_flow(self, request):
        bearer = get_or_create_jwt()
        request.headers["Authorization"] = f"Bearer {bearer}"
        response = yield request
        if response.status_code == 401:
            bearer = get_or_create_jwt(force=True)
            request.headers["Authorization"] = f"Bearer {bearer}"
            yield request


async def get_installation(account):
    owner = account["login"]
    account_type = "users" if account["type"].lower() == "user" else "orgs"
    url = f"{config.GITHUB_API_URL}/{account_type}/{owner}/installation"
    async with http.AsyncClient(
        auth=GithubBearerAuth(), **http.DEFAULT_CLIENT_OPTIONS
    ) as client:
        try:
            installation = (await client.get(url)).json()
            permissions_need_to_be_updated(installation)
            return installation
        except http.HTTPNotFound as e:
            LOG.debug(
                "Mergify not installed",
                gh_owner=owner,
                error_message=e.message,
            )
            raise exceptions.MergifyNotInstalled()
