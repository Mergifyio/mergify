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
import functools

import daiquiri
from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine.clients import common
from mergify_engine.clients import github_app


LOG = daiquiri.getLogger(__name__)


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
            self._access_token = r.json()["token"]
            self._access_token_expiration = datetime.fromisoformat(
                r.json()["expires_at"][:-1]
            )  # Remove the Z
        return self._access_token


class GithubInstallationClient(common.BaseClient):
    def __init__(self, owner, repo, installation):
        self.owner = owner
        self.repo = repo
        self.installation = installation
        super().__init__(
            base_url=f"https://api.{config.GITHUB_DOMAIN}/repos/{owner}/{repo}/",
            auth=GithubInstallationAuth(self.installation["id"]),
            **common.DEFAULT_CLIENT_OPTIONS,
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_api_version(getattr(self, method)))

    def __repr__(self):
        return f"<GithubInstallationClient owner='{self.owner}' repo='{self.repo}' installation_id={self.installation_id}>"

    @staticmethod
    def _inject_api_version(func):
        @functools.wraps(func)
        def wrapper(url, api_version=None, **kwargs):
            headers = kwargs.pop("headers", {})
            if api_version:
                headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"
            return func(url, headers=headers, **kwargs)

        return wrapper

    def item(self, url, api_version=None, **params):
        return self.get(url, api_version=api_version, params=params).json()

    def items(self, url, api_version=None, list_items=None, **params):
        while True:
            r = self.get(url, api_version=api_version, params=params)
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
        now = datetime.utcnow()
        delta = reset - now
        statsd.increment("engine.rate_limited")
        raise exceptions.RateLimited(delta.total_seconds(), rate)
    return client


def get_installation(owner, repo, installation_id=None):
    installation = github_app.get_client().get_installation(owner, repo)
    if installation_id is not None and installation["id"] != installation_id:
        LOG.error(
            "installation id for repository diff from event installation id",
            gh_owner=owner,
            gh_repo=repo,
            installation_id=installation["id"],
            expected_installation_id=installation_id,
        )
    return installation
