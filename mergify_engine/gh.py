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


import jwt
import time

import httpx
import tenacity

from mergify_engine import config

DEFAULT_ACCEPT = "application/vnd.github.machine-man-preview+json"


class GithubAuth(httpx.Auth):
    # TODO(sileht): When the whole code will be async and we didn't use celery and
    # processes anymore, a GithubAuth can be keep in memory between ran tasks. This will
    # reduce the token rate creation a lot, token will be renew only when needed.

    JWT_EXPIRATION = 60

    def __init__(self, client, installation_id):
        self.client = client
        self.installation_id = installation_id

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(httpx.exceptions.HttpError),
        stop=tenacity.stop_after_attempt(3),
    )
    def auth_flow(self, request):
        if self.access_token is None:
            self.access_token = self.get_access_token()
        request.headers["Authentication"] = f"token {self.access_token['token']}"
        response = yield request
        if response.status_code == 401:
            self.token = None
            response.raise_for_status()

    @classmethod
    def create_jwt(cls):
        now = int(time.time())
        payload = {
            "iat": now,
            "exp": now + cls.JWT_EXPIRATION,
            "iss": config.INTEGRATION_ID,
        }
        encrypted = jwt.encode(payload, key=config.PRIVATE_KEY, algorithm="RS256")
        return encrypted.decode("utf-8")

    def get_access_token(self):
        r = self.client.get(
            f"/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.create_jwt()}"},
        )
        r.raise_for_status()
        return r.json()


class GithubClient(httpx.Client):
    """
    The idea here is to have only one living HTTP client connected to Github, so we can
    control the connection pooling.

    This class also handle authentification and token renewal automatically when needed.
    """

    def __init__(self):
        self.auths = {}
        self.client = httpx.Client(
            base_url=config.GITHUB_DOMAIN,
            headers={"Accept": DEFAULT_ACCEPT, "User-Agent": "Mergify/Python"},
            trust_env=False,
        )

        for verb in ("get", "post", "put", "patch", "options", "head", "request"):
            setattr(self, verb, self._verb_wrapper(verb))

    @staticmethod
    def _verb_wrapper(verb):
        def verb(self, installation_id, *args, **kwargs):
            kwargs["auth"] = self._get_auth(installation_id)
            return getattr(self.client, verb)(*args, **kwargs)

        return verb

    def _get_auth(self, installation_id):
        if installation_id not in self.auths:
            self.auths[installation_id] = GithubAuth(self.client, installation_id)
        return self.auths[installation_id]

    def close(self):
        self.client.close()


GH = GithubClient()


def get_pulls(
    installation_id,
    owner,
    repo,
    *,
    state="open",
    head=None,
    base=None,
    sort="created",
    direction="asc",
):
    url = f"/repos/{owner}/{repo}/pulls"
    while url:
        r = GH(installation_id).get(url)
        if "next" in r.links:
            url = r.links["next"]["url"]
    return r.json()
