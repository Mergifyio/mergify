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
import typing
from urllib import parse

import daiquiri
from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine.clients import github_app
from mergify_engine.clients import http


RATE_LIMIT_THRESHOLD = 20
LOGGING_REQUESTS_THRESHOLD = 10

LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class TooManyPages(Exception):
    last_page: int
    response: httpx.Response


@dataclasses.dataclass
class CachedToken:
    STORAGE: typing.ClassVar[typing.Dict[int, typing.Any]] = {}

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
        self.permissions_need_to_be_updated = False
        self.installation = {
            "id": config.ACTION_ID,
        }
        # TODO(sileht): To be defined when we handle subscription for GitHub Action
        self.owner = None
        self.owner_id = 0

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {config.GITHUB_TOKEN}"
        yield request


class GithubTokenAuth(httpx.Auth):
    def __init__(self, owner, token, owner_id=None):
        self._token = token
        self.owner = owner
        self.owner_id = owner_id
        self.permissions_need_to_be_updated = False

    @property
    def installation(self):
        raise RuntimeError(
            f"{self.__class__.__name__} must not be used for installation dependent code"
        )

    @contextlib.contextmanager
    def response_body_read(self):
        self.requires_response_body = True
        try:
            yield
        finally:
            self.requires_response_body = False

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {self._token}"
        if self.owner_id is None:
            with self.response_body_read():
                user_response = yield self.build_request(
                    "GET", f"{config.GITHUB_API_URL}/users/{self.owner}"
                )
                http.raise_for_status(user_response)
                self.owner_id = user_response.json()["id"]
        yield request

    def build_request(self, method, url):
        headers = http.DEFAULT_CLIENT_OPTIONS["headers"].copy()
        headers["Authorization"] = f"token {self._token}"
        return httpx.Request(method, url, headers=headers)


class GithubAppInstallationAuth(httpx.Auth):
    def __init__(self, owner):
        self.owner = owner

        self._cached_token = None
        self.installation = None
        self.owner_id = None
        self.permissions_need_to_be_updated = None

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
                        "Mergify not installed",
                        gh_owner=self.owner,
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

            if auth_response.status_code == 403:
                error_message = auth_response.json()["message"]
                if "This installation has been suspended" in error_message:
                    LOG.debug(
                        "Mergify installation suspended",
                        gh_owner=self.owner,
                        error_message=error_message,
                    )
                    raise exceptions.MergifyNotInstalled()

            http.raise_for_status(auth_response)
            token = self._set_access_token(auth_response.json())

        request.headers["Authorization"] = f"token {token}"
        yield request

    def build_installation_request(self, url=None, force=False):
        if url is None:
            url = f"{config.GITHUB_API_URL}/users/{self.owner}/installation"
        return self.build_github_app_request("GET", url, force=force)

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
        self.installation = installation_response.json()
        self.owner_id = self.installation["account"]["id"]
        self.permissions_need_to_be_updated = github_app.permissions_need_to_be_updated(
            self.installation
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
        else:
            raise RuntimeError("get_access_token() call on an unused client")


_T_get_auth = typing.Union[GithubAppInstallationAuth, GithubActionAccessTokenAuth]


def get_auth(owner: typing.Optional[str]) -> _T_get_auth:
    if config.GITHUB_APP:
        return GithubAppInstallationAuth(owner)
    else:
        return GithubActionAccessTokenAuth()


class AsyncGithubInstallationClient(http.AsyncClient):
    auth: _T_get_auth

    def __init__(self, auth: _T_get_auth):
        self._requests: typing.List[typing.Tuple[str, str]] = []
        super().__init__(
            base_url=config.GITHUB_API_URL,
            auth=auth,
            **http.DEFAULT_CLIENT_OPTIONS,  # type: ignore
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_options(getattr(self, method)))

    def __repr__(self):
        return f"<AsyncGithubInstallationClient owner='{self.auth.owner}'>"

    def _inject_options(self, func):
        @functools.wraps(func)
        async def wrapper(url, api_version=None, oauth_token=None, **kwargs):
            headers = kwargs.pop("headers", {})
            if api_version:
                headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"
            if oauth_token:
                kwargs["auth"] = GithubTokenAuth(
                    self.auth.owner, oauth_token, self.auth.owner_id
                )
            return await func(url, headers=headers, **kwargs)

        return wrapper

    async def item(self, url, api_version=None, oauth_token=None, **params):
        response = await self.get(
            url, api_version=api_version, oauth_token=oauth_token, params=params
        )
        return response.json()

    async def items(
        self, url, api_version=None, oauth_token=None, list_items=None, **params
    ):
        while True:
            response = await self.get(
                url, api_version=api_version, oauth_token=oauth_token, params=params
            )
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
                await self.check_rate_limit()
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
        self._generate_metrics()

    async def __aexit__(self, exc_type, exc_value, traceback):
        await super().__aexit__(exc_type, exc_value, traceback)
        self._generate_metrics()

    def _generate_metrics(self):
        nb_requests = len(self._requests)
        statsd.histogram(
            "http.client.session",
            nb_requests,
            tags=[f"hostname:{self.base_url.host}"],
        )
        if nb_requests >= LOGGING_REQUESTS_THRESHOLD:
            LOG.warning(
                "number of GitHub requests for this session crossed the threshold (%s): %s",
                LOGGING_REQUESTS_THRESHOLD,
                nb_requests,
                gh_owner=self.auth.owner,
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


async def aget_client(
    owner: typing.Optional[str] = None, auth: typing.Optional[_T_get_auth] = None
) -> AsyncGithubInstallationClient:
    client = AsyncGithubInstallationClient(auth or get_auth(owner))
    await client.check_rate_limit()
    return client


class GithubInstallationClient(http.Client):
    auth: _T_get_auth

    def __init__(self, auth: _T_get_auth):
        self._requests: typing.List[typing.Tuple[str, str]] = []
        super().__init__(
            base_url=config.GITHUB_API_URL,
            auth=auth,
            **http.DEFAULT_CLIENT_OPTIONS,  # type: ignore
        )

        for method in ("get", "post", "put", "patch", "delete", "head"):
            setattr(self, method, self._inject_options(getattr(self, method)))

    def __repr__(self):
        return f"<GithubInstallationClient owner='{self.auth.owner}'"

    def _inject_options(self, func):
        @functools.wraps(func)
        def wrapper(url, api_version=None, oauth_token=None, **kwargs):
            headers = kwargs.pop("headers", {})
            if api_version:
                headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"
            if oauth_token:
                kwargs["auth"] = GithubTokenAuth(
                    self.auth.owner, oauth_token, self.auth.owner_id
                )

            return func(url, headers=headers, **kwargs)

        return wrapper

    def item(self, url, api_version=None, oauth_token=None, **params):
        return self.get(
            url, api_version=api_version, oauth_token=oauth_token, params=params
        ).json()

    def items(self, url, api_version=None, oauth_token=None, list_items=None, **params):
        while True:
            r = self.get(
                url, api_version=api_version, oauth_token=oauth_token, params=params
            )
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
        self._generate_metrics()

    def __exit__(self, exc_type, exc_value, traceback):
        super().__exit__(exc_type, exc_value, traceback)
        self._generate_metrics()

    def _generate_metrics(self):
        nb_requests = len(self._requests)
        statsd.histogram(
            "http.client.session",
            nb_requests,
            tags=[f"hostname:{self.base_url.host}"],
        )
        if nb_requests >= LOGGING_REQUESTS_THRESHOLD:
            LOG.warning(
                "number of GitHub requests for this session crossed the threshold (%s): %s",
                LOGGING_REQUESTS_THRESHOLD,
                nb_requests,
                gh_owner=self.auth.owner,
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


def get_client(
    owner: typing.Optional[str] = None, auth: typing.Optional[_T_get_auth] = None
) -> GithubInstallationClient:
    client = GithubInstallationClient(auth or get_auth(owner))
    client.check_rate_limit()
    return client
