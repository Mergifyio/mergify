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


import contextlib
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


class GithubActionAccessTokenAuth(httpx.Auth):
    def __init__(self):
        self._installation = {
            "id": config.ACTION_ID,
            "permissions_need_to_be_updated": False,
        }

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {config.GITHUB_TOKEN}"
        yield request


class GithubAppInstallationAuth(httpx.Auth):
    def __init__(self, owner, repo):
        self.owner = owner
        self.repo = repo

        self._cached_token = None
        self.installation = None

    @contextlib.contextmanager
    def response_body_read(self):
        self.requires_response_body = True
        try:
            yield
        finally:
            self.requires_response_body = False

    def auth_flow(self, request):
        if self.installation is None:
            with self.response_body_read():
                installation_response = yield self.build_installation_request()
                if installation_response.status_code == 401:  # due to jwt
                    installation_response = yield self.build_installation_request(
                        force=True
                    )
                if installation_response.is_redirect:
                    installation_response = yield self.build_installation_request(
                        url=installation_response.headers["Location"],
                    )
                if installation_response.status_code == 404:
                    LOG.debug(
                        "mergify not installed or repository not found",
                        gh_owner=self.owner,
                        gh_repo=self.repo,
                        error_message=installation_response.json()["message"],
                    )
                    raise exceptions.MergifyNotInstalled()

                http.raise_for_status(installation_response)

                self._set_installation(installation_response)

        token = self._get_access_token()
        if token:
            request.headers["Authorization"] = f"token {token}"
            response = yield request
            if response.status_code != 401:  # due to access_token
                return

        with self.response_body_read():
            auth_response = yield self.build_access_token_request()
            if auth_response.status_code == 401:  # due to jwt
                auth_response = yield self.build_access_token_request(force=True)
            http.raise_for_status(auth_response)
            token = self._set_access_token(auth_response.json())

        request.headers["Authorization"] = f"token {token}"
        yield request

    def build_installation_request(self, url=None, force=False):
        if url is None:
            url = (
                f"{config.GITHUB_API_URL}/repos/{self.owner}/{self.repo}/installation",
            )
        return self.build_github_app_request(
            "GET",
            f"{config.GITHUB_API_URL}/repos/{self.owner}/{self.repo}/installation",
            force=force,
        )

    def build_access_token_request(self, force=False):
        return self.build_github_app_request(
            "POST",
            f"{config.GITHUB_API_URL}/app/installations/{self.installation['id']}/access_tokens",
            force=force,
        )

    def build_github_app_request(self, method, url, force=False):
        headers = http.DEFAULT_CLIENT_OPTIONS["headers"].copy()
        headers["Authorization"] = f"Bearer {github_app.get_or_create_jwt(force)}"
        return httpx.Request(method, url, headers=headers)

    def _set_installation(self, installation_response):
        self.installation = github_app.validate_installation(
            installation_response.json()
        )
        self._cached_token = CachedToken.get(self.installation["id"])

    def _set_access_token(self, data):
        self._cached_token = CachedToken(
            self.installation["id"],
            data["token"],
            datetime.fromisoformat(data["expires_at"][:-1]),  # Remove the Z
        )
        LOG.info(
            "New token acquired",
            gh_owner=self.owner,
            gh_repo=self.repo,
            expire_at=self._cached_token.expiration,
        )
        return self._cached_token.token

    def _get_access_token(self):
        now = datetime.utcnow()
        if not self._cached_token:
            return None
        elif self._cached_token.expiration <= now:
            LOG.info(
                "Token expired",
                gh_owner=self.owner,
                gh_repo=self.repo,
                expire_at=self._cached_token.expiration,
            )
            self._cached_token.invalidate()
            self._cached_token = None
            return None
        else:
            return self._cached_token.token

    def get_access_token(self):
        """Legacy method for backport/copy actions"""
        token = self._get_access_token()
        if token:
            return token
        r = github_app.get_client().post(
            f"/app/installations/{self.installation['id']}/access_tokens"
        )
        return self._set_access_token(r.json())


def get_auth(owner, repo):
    if config.GITHUB_APP:
        return GithubAppInstallationAuth(owner, repo)
    else:
        return GithubActionAccessTokenAuth()


class AsyncGithubInstallationClient(http.AsyncClient):
    def __init__(self, owner, repo):
        self.owner = owner
        self.repo = repo
        self._requests = []

        super().__init__(
            base_url=f"{config.GITHUB_API_URL}/repos/{owner}/{repo}/",
            auth=get_auth(owner, repo),
            **http.DEFAULT_CLIENT_OPTIONS,
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_api_version(getattr(self, method)))

    def __repr__(self):
        return (
            f"<AsyncGithubInstallationClient owner='{self.owner}' repo='{self.repo}'>"
        )

    @staticmethod
    def _inject_api_version(func):
        @functools.wraps(func)
        async def wrapper(url, api_version=None, **kwargs):
            headers = kwargs.pop("headers", {})
            if api_version:
                headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"
            return await func(url, headers=headers, **kwargs)

        return wrapper

    async def item(self, url, api_version=None, **params):
        response = await self.get(url, api_version=api_version, params=params)
        return response.json()

    async def items(self, url, api_version=None, list_items=None, **params):
        while True:
            response = await self.get(url, api_version=api_version, params=params)
            last_url = response.links.get("last", {}).get("url")
            if last_url:
                last_page = int(
                    parse.parse_qs(parse.urlparse(last_url).query).get("page")[0]
                )
                if last_page > 100:
                    raise TooManyPages(last_page, response)

            items = response.json()
            if list_items:
                items = items[list_items]
            for item in items:
                yield item
            if "next" in response.links:
                url = response.links["next"]["url"]
                params = None
            else:
                break

    async def request(self, method, url, *args, **kwargs):
        reply = None
        try:
            reply = await super().request(method, url, *args, **kwargs)
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

    async def aclose(self):
        await super().aclose()
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

    async def check_rate_limit(self):
        response = await self.item("/rate_limit")
        rate = response["resources"]
        if rate["core"]["remaining"] < RATE_LIMIT_THRESHOLD:
            reset = datetime.utcfromtimestamp(rate["core"]["reset"])
            now = datetime.utcnow()
            delta = reset - now
            statsd.increment(
                "http.client.rate_limited", tags=[f"hostname:{self.base_url.host}"]
            )
            raise exceptions.RateLimited(delta.total_seconds(), rate)


async def aget_client(*args, **kwargs):
    client = AsyncGithubInstallationClient(*args, **kwargs)
    await client.check_rate_limit()
    return client


async def aget_installation_by_id(installation_id):
    client = github_app.aget_client()
    return await client.get_installation_by_id(installation_id)


class GithubInstallationClient(http.Client):
    def __init__(self, owner, repo):
        self.owner = owner
        self.repo = repo
        self._requests = []
        super().__init__(
            base_url=f"{config.GITHUB_API_URL}/repos/{owner}/{repo}/",
            auth=get_auth(owner, repo),
            **http.DEFAULT_CLIENT_OPTIONS,
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_api_version(getattr(self, method)))

    def __repr__(self):
        return f"<GithubInstallationClient owner='{self.owner}' repo='{self.repo}'>"

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


def get_github_action_installation():
    return {
        "id": config.ACTION_ID,
        "permissions_need_to_be_updated": False,
    }


def get_installation_by_id(installation_id):
    if config.GITHUB_APP:
        return github_app.get_client().get_installation_by_id(installation_id)
    elif installation_id == config.ACTION_ID:
        return {
            "id": config.ACTION_ID,
            "permissions_need_to_be_updated": False,
        }
    else:
        raise RuntimeError("Unexpected installation id")
