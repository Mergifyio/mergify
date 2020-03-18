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

import httpx
import jwt

from mergify_engine import config
from mergify_engine.clients import common


class GithubBearerAuth(httpx.Auth):
    JWT_EXPIRATION = 60

    def __init__(self):
        self.jwt = None
        self.jwt_expiration = None

    def get_or_create_jwt(self):
        now = int(time.time())

        if self.jwt is None or self.jwt_expiration <= now:
            self.jwt_expiration = now + self.JWT_EXPIRATION
            payload = {
                "iat": now,
                "exp": self.jwt_expiration,
                "iss": config.INTEGRATION_ID,
            }
            encrypted = jwt.encode(payload, key=config.PRIVATE_KEY, algorithm="RS256")
            self.jwt = encrypted.decode("utf-8")

        return self.jwt

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.get_or_create_jwt()}"
        response = yield request
        if response.status_code == 401:
            self.jwt = None
            request.headers["Authorization"] = f"Bearer {self.get_or_create_jwt()}"
            yield request


class _Client(httpx.Client, common.HttpxHelpersMixin):
    def __init__(self):
        super().__init__(
            base_url=f"https://api.{config.GITHUB_DOMAIN}",
            auth=GithubBearerAuth(),
            **common.DEFAULT_CLIENT_OPTIONS,
        )

    def get_installation_id(self, owner, repo=None, account_type=None):
        if not account_type and not repo:
            raise RuntimeError("repo or account_type must be passed")

        if repo:
            url = f"/repos/{owner}/{repo}/installation"
        else:
            account_type = "users" if account_type.lower() == "user" else "orgs"
            url = f"/{account_type}/{owner}/installation"

        response = self.get(url)
        if response.status_code == 200:
            return response.json()["id"]
        response.raise_for_status()


global _GITHUB_APP
global _GITHUB_APP_LOCK
_GITHUB_APP = None
_GITHUB_APP_LOCK = threading.Lock()


def get_client():
    global _GITHUB_APP
    global _GITHUB_APP_LOCK

    if not _GITHUB_APP:
        with _GITHUB_APP_LOCK:
            if not _GITHUB_APP:
                _GITHUB_APP = _Client()

    return _GITHUB_APP
