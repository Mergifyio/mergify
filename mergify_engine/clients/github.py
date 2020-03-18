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


from datetime import datetime

from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine.clients import common
from mergify_engine.clients import github_app


class GithubInstallationAuth(httpx.Auth):
    def __init__(self, installation_id):
        self.installation_id = installation_id
        self._access_token = None
        self._access_token_expiration = None

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {self.get_access_token()}"
        response = yield request
        if response.status_code == 401:
            self._access_token = None
            self._access_token_expiration = None
            request.headers["Authorization"] = f"token {self.get_access_token()}"
            yield request

    def get_access_token(self):
        now = datetime.utcnow()
        if self._access_token is None or self._access_token_expiration <= now:
            r = github_app.get_client().post(
                f"/app/installations/{self.installation_id}/access_tokens",
            )
            r.raise_for_status()
            self._access_token = r.json()["token"]
            self._access_token_expiration = datetime.fromisoformat(
                r.json()["expires_at"][:-1]
            )  # Remove the Z
        return self._access_token


class GithubInstallationClient(httpx.Client, common.HttpxRetriesMixin):
    def __init__(self, owner, repo, installation_id=None):
        self.owner = owner
        self.repo = repo
        self.installation_id = installation_id or github_app.get_client().get_installation_id(
            owner, repo
        )
        super().__init__(
            base_url=f"https://api.{config.GITHUB_DOMAIN}/repos/{owner}/{repo}/",
            auth=GithubInstallationAuth(self.installation_id),
            **common.DEFAULT_CLIENT_OPTIONS,
        )

    def item(self, url, api_version=None, **params):
        headers = {}
        if api_version:
            headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"

        r = self.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    def items(self, url, api_version=None, list_items=None, **params):
        headers = {}
        if api_version:
            headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"

        while True:
            r = self.get(url, params=params, headers=headers)
            r.raise_for_status()
            items = r.json()
            if list_items:
                items = items[list_items]
            for item in items:
                yield item
            if "next" in r.links:
                url = r.links["next"]["url"]
                params = None
            else:
                break


RATE_LIMIT_THRESHOLD = 20


def get_client(*args, **kwargs):
    client = GithubInstallationClient(*args, **kwargs)
    rate = client.item("/rate_limit")["resources"]
    if rate["core"]["remaining"] < RATE_LIMIT_THRESHOLD:
        reset = datetime.utcfromtimestamp(rate["core"]["reset"])
        now = datetime.datetime.utcnow()
        delta = reset - now
        statsd.increment("engine.rate_limited")
        raise exceptions.RateLimited(delta.total_seconds(), rate)
    return client
