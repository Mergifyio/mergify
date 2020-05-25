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


import dataclasses
from datetime import datetime
import functools
from urllib import parse

from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine.clients import github_app
from mergify_engine.clients import http


RATE_LIMIT_THRESHOLD = 20
LOGGING_REQUESTS_THRESHOLD = 20
LOG = logs.getLogger(__name__)


@dataclasses.dataclass
class TooManyPages(Exception):
    last_page: int
    response: httpx.Response


@dataclasses.dataclass
class CachedToken:
    STORAGE = {}

    installation_id: int
    token: dict
    expiration: datetime

    def __post_init__(self):
        CachedToken.STORAGE[self.installation_id] = self

    @classmethod
    def get(cls, installation_id):
        return cls.STORAGE.get(installation_id)

    def invalidate(self):
        CachedToken.STORAGE.pop(self.installation_id, None)


class GithubInstallationAuth(httpx.Auth):
    def __init__(self, installation_id, owner, repo):
        self.installation_id = installation_id
        self.owner = owner
        self.repo = repo
        self._cached_token = CachedToken.get(self.installation_id)

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {self.get_access_token()}"
        response = yield request
        if response.status_code == 401:
            LOG.info(
                "Token expired",
                gh_owner=self.owner,
                gh_repo=self.repo,
                expire_at=self._cached_token.expiration,
            )
            self._cached_token.invalidate()
            self._cached_token = None
            request.headers["Authorization"] = f"token {self.get_access_token()}"
            yield request

    def get_access_token(self):
        now = datetime.utcnow()
        if self._cached_token is None or self._cached_token.expiration <= now:
            r = github_app.get_client().post(
                f"/app/installations/{self.installation_id}/access_tokens",
            )
            self._cached_token = CachedToken(
                self.installation_id,
                r.json()["token"],
                datetime.fromisoformat(r.json()["expires_at"][:-1]),  # Remove the Z
            )
            LOG.info(
                "New token acquired",
                gh_owner=self.owner,
                gh_repo=self.repo,
                expire_at=self._cached_token.expiration,
            )
        return self._cached_token.token


class GithubInstallationClient(http.Client):
    def __init__(self, owner, repo, installation):
        self.owner = owner
        self.repo = repo
        self.installation = installation
        self._requests = []
        super().__init__(
            base_url=f"{config.GITHUB_API_URL}/repos/{owner}/{repo}/",
            auth=GithubInstallationAuth(self.installation["id"], owner, repo),
            **http.DEFAULT_CLIENT_OPTIONS,
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_api_version(getattr(self, method)))

    def __repr__(self):
        return f"<GithubInstallationClient owner='{self.owner}' repo='{self.repo}' installation_id={self.installation['id']}>"

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
            last_url = r.links.get("last", {}).get("url")
            if last_url:
                last_page = int(
                    parse.parse_qs(parse.urlparse(last_url).query).get("page")[0]
                )
                if last_page > 100:
                    raise TooManyPages(last_page, r)

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

    def request(self, method, url, *args, **kwargs):
        reply = None
        try:
            reply = super().request(method, url, *args, **kwargs)
        except http.HTTPClientSideError as e:
            if e.status_code == 403:
                # TODO(sileht): Maybe we could look at header to avoid a request:
                # they should be according the doc:
                #  X-RateLimit-Limit: 60
                #  X-RateLimit-Remaining: 0
                #  X-RateLimit-Reset: 1377013266
                self.check_rate_limit()
            raise
        finally:
            if reply is None:
                status_code = "error"
            else:
                status_code = reply.status_code
            statsd.increment(
                "http.client.requests",
                tags=[f"hostname:{self.base_url.host}", f"status_code:{status_code}"],
            )
            self._requests.append((method, url))
        return reply

    def close(self):
        super().close()
        nb_requests = len(self._requests)
        statsd.histogram(
            "http.client.session", nb_requests, tags=[f"hostname:{self.base_url.host}"],
        )
        if nb_requests >= LOGGING_REQUESTS_THRESHOLD:
            LOG.warning(
                "number of GitHub requests for this session crossed the threshold (%s): %s",
                LOGGING_REQUESTS_THRESHOLD,
                nb_requests,
                gh_owner=self.owner,
                gh_repo=self.repo,
                requests=self._requests,
            )
        self._requests = []

    def check_rate_limit(self):
        rate = self.item("/rate_limit")["resources"]
        if rate["core"]["remaining"] < RATE_LIMIT_THRESHOLD:
            reset = datetime.utcfromtimestamp(rate["core"]["reset"])
            now = datetime.utcnow()
            delta = reset - now
            statsd.increment(
                "http.client.rate_limited", tags=[f"hostname:{self.base_url.host}"]
            )
            raise exceptions.RateLimited(delta.total_seconds(), rate)


def get_client(*args, **kwargs):
    client = GithubInstallationClient(*args, **kwargs)
    client.check_rate_limit()
    return client


def get_installation_by_id(installation_id):
    return github_app.get_client().get_installation_by_id(installation_id)


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
