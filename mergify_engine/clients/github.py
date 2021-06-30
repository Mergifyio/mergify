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
import datetime
import typing
from urllib import parse

import daiquiri
from datadog import statsd
import httpx

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine.clients import github_app
from mergify_engine.clients import http


RATE_LIMIT_THRESHOLD = 20
LOGGING_REQUESTS_THRESHOLD = 40
LOGGING_REQUESTS_THRESHOLD_ABSOLUTE = 400

LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class TooManyPages(Exception):
    last_page: int
    response: httpx.Response


@dataclasses.dataclass
class CachedToken:
    STORAGE: typing.ClassVar[
        typing.Dict[github_types.GitHubInstallationIdType, "CachedToken"]
    ] = {}

    installation_id: github_types.GitHubInstallationIdType
    token: str
    expiration: datetime.datetime

    def __post_init__(self):
        CachedToken.STORAGE[self.installation_id] = self

    @classmethod
    def get(
        cls, installation_id: github_types.GitHubInstallationIdType
    ) -> typing.Optional["CachedToken"]:
        return cls.STORAGE.get(installation_id)

    def invalidate(self) -> None:
        CachedToken.STORAGE.pop(self.installation_id, None)


class GithubActionAccessTokenAuth(httpx.Auth):
    owner_id: github_types.GitHubAccountIdType
    owner: typing.Optional[github_types.GitHubLogin]

    def __init__(self) -> None:
        self.permissions_need_to_be_updated = False
        self.installation = {
            "id": config.ACTION_ID,
        }
        # TODO(sileht): To be defined when we handle subscription for GitHub Action
        self.owner = None
        self.owner_id = github_types.GitHubAccountIdType(0)

    def auth_flow(self, request):
        request.headers["Authorization"] = f"token {config.GITHUB_TOKEN}"
        yield request

    def get_access_token(self):
        """Legacy method for backport/copy actions"""
        raise NotImplementedError


class GithubTokenAuth(httpx.Auth):
    owner_id: typing.Optional[github_types.GitHubAccountIdType]
    owner: typing.Optional[github_types.GitHubLogin]

    def __init__(
        self,
        token: str,
        owner: typing.Optional[github_types.GitHubLogin] = None,
        owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
    ) -> None:
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
        if self.owner_id is None or self.owner is None:
            with self.response_body_read():
                user_response = yield self.build_request(
                    "GET", f"{config.GITHUB_API_URL}/user"
                )
                http.raise_for_status(user_response)
                account = typing.cast(github_types.GitHubAccount, user_response.json())
                self.owner_id = account["id"]
                self.owner = account["login"]
        yield request

    def build_request(self, method, url):
        headers = http.DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"token {self._token}"
        return httpx.Request(method, url, headers=headers)

    def get_access_token(self) -> str:
        return self._token


class GithubAppInstallationAuth(httpx.Auth):

    installation: typing.Optional[github_types.GitHubInstallation]

    def __init__(
        self,
        owner_name: typing.Optional[github_types.GitHubLogin] = None,
        owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
    ) -> None:
        self.owner = owner_name
        self.owner_id = owner_id

        self._cached_token: typing.Optional[CachedToken] = None
        self.installation = None
        self.permissions_need_to_be_updated = False

    @contextlib.contextmanager
    def response_body_read(self):
        self.requires_response_body = True
        try:
            yield
        finally:
            self.requires_response_body = False

    def auth_flow(
        self, request: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
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
                        gh_owner_id=self.owner_id,
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

            if auth_response.status_code == 404:
                raise exceptions.MergifyNotInstalled()
            elif auth_response.status_code == 403:
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

    def build_installation_request(
        self, url: typing.Optional[str] = None, force: bool = False
    ) -> httpx.Request:
        if url is None:
            if self.owner_id:
                url = f"{config.GITHUB_API_URL}/user/{self.owner_id}/installation"
            else:
                url = f"{config.GITHUB_API_URL}/users/{self.owner}/installation"
        return self.build_github_app_request("GET", url, force=force)

    def build_access_token_request(self, force: bool = False) -> httpx.Request:
        if self.installation is None:
            raise RuntimeError("No installation")

        return self.build_github_app_request(
            "POST",
            f"{config.GITHUB_API_URL}/app/installations/{self.installation['id']}/access_tokens",
            force=force,
        )

    def build_github_app_request(
        self, method: str, url: str, force: bool = False
    ) -> httpx.Request:
        headers = http.DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"Bearer {github_app.get_or_create_jwt(force)}"  # type: ignore[no-untyped-call]
        return httpx.Request(method, url, headers=headers)

    def _set_installation(self, installation_response: httpx.Response) -> None:
        self.installation = typing.cast(
            github_types.GitHubInstallation, installation_response.json()
        )
        self.owner_id = self.installation["account"]["id"]
        self.owner = self.installation["account"]["login"]
        self.permissions_need_to_be_updated = github_app.permissions_need_to_be_updated(
            self.installation
        )
        self._cached_token = CachedToken.get(self.installation["id"])

    def _set_access_token(
        self, data: github_types.GitHubInstallationAccessToken
    ) -> str:
        if self.installation is None:
            raise RuntimeError("Cannot set access token, no installation")

        self._cached_token = CachedToken(
            self.installation["id"],
            data["token"],
            datetime.datetime.fromisoformat(data["expires_at"][:-1]),  # Remove the Z
        )
        LOG.info(
            "New token acquired",
            gh_owner=self.owner,
            expire_at=self._cached_token.expiration,
        )
        return self._cached_token.token

    def _get_access_token(self) -> typing.Optional[str]:
        now = datetime.datetime.utcnow()
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

    def get_access_token(self) -> str:
        """Legacy method for backport/copy actions"""
        token = self._get_access_token()
        if token:
            return token
        else:
            raise RuntimeError("get_access_token() call on an unused client")


_T_get_auth = typing.Union[
    GithubAppInstallationAuth, GithubActionAccessTokenAuth, GithubTokenAuth
]


def get_auth(
    owner_name: typing.Optional[github_types.GitHubLogin] = None,
    owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
) -> _T_get_auth:
    if config.GITHUB_APP:
        if owner_name is None and owner_id is None:
            raise ValueError("No owner provided")
        return GithubAppInstallationAuth(owner_name=owner_name, owner_id=owner_id)
    else:
        return GithubActionAccessTokenAuth()


def _check_rate_limit(response: httpx.Response) -> None:
    remaining = response.headers.get("X-RateLimit-Remaining")

    if remaining is None:
        return

    remaining = int(remaining)

    if remaining < RATE_LIMIT_THRESHOLD:
        reset = response.headers.get("X-RateLimit-Reset")
        if reset is None:
            delta = datetime.timedelta(minutes=5)
        else:
            delta = (
                datetime.datetime.utcfromtimestamp(int(reset))
                - datetime.datetime.utcnow()
            )
        if response.url is not None:
            statsd.increment(
                "http.client.rate_limited",
                tags=[f"hostname:{response.url.host}"],
            )
        raise exceptions.RateLimited(delta, remaining)


def _inject_options(func: typing.Any) -> typing.Any:
    async def wrapper(
        self: "AsyncGithubInstallationClient",
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> typing.Any:
        headers = kwargs.pop("headers", {})
        if api_version:
            headers["Accept"] = f"application/vnd.github.{api_version}-preview+json"
        if oauth_token:
            kwargs["auth"] = GithubTokenAuth(
                oauth_token, self.auth.owner, self.auth.owner_id
            )
        return await func(url, headers=headers, **kwargs)

    return wrapper


class AsyncGithubInstallationClient(http.AsyncClient):
    auth: _T_get_auth

    def __init__(self, auth: _T_get_auth):
        self._requests: typing.List[typing.Tuple[str, str]] = []
        self._requests_ratio: int = 1
        super().__init__(
            base_url=config.GITHUB_API_URL,
            auth=auth,
            **http.DEFAULT_CLIENT_OPTIONS,  # type: ignore
        )

    def __repr__(self):
        return f"<AsyncGithubInstallationClient owner='{self.auth.owner}'>"

    def _prepare_request_kwargs(
        self,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> typing.Any:
        if api_version:
            kwargs.setdefault("headers", {})[
                "Accept"
            ] = f"application/vnd.github.{api_version}-preview+json"
        if oauth_token:
            kwargs["auth"] = GithubTokenAuth(
                oauth_token, self.auth.owner, self.auth.owner_id
            )
        return kwargs

    async def get(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().get(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def post(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().post(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def put(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().put(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def patch(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().patch(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def head(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().head(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def delete(  # type: ignore[override]
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        **kwargs: typing.Any,
    ) -> httpx.Response:
        return await super().delete(
            url, **self._prepare_request_kwargs(api_version, oauth_token, **kwargs)
        )

    async def item(
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        params: typing.Optional[typing.Dict[str, str]] = None,
    ) -> typing.Any:
        response = await self.get(
            url, api_version=api_version, oauth_token=oauth_token, params=params
        )
        return response.json()

    async def items(
        self,
        url: str,
        api_version: typing.Optional[github_types.GitHubApiVersion] = None,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
        list_items: typing.Optional[str] = None,
        params: typing.Optional[typing.Dict[str, str]] = None,
    ) -> typing.Any:

        # NOTE(sileht): can't be on the same line...
        # https://github.com/python/mypy/issues/10743
        final_params: typing.Optional[typing.Dict[str, str]]
        final_params = {"per_page": "100"}

        if params is not None:
            final_params.update(params)

        while True:
            response = await self.get(
                url,
                api_version=api_version,
                oauth_token=oauth_token,
                params=final_params,
            )
            last_url = response.links.get("last", {}).get("url")
            if last_url:
                last_page = int(
                    parse.parse_qs(parse.urlparse(last_url).query)["page"][0]
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
                final_params = None
            else:
                break

    async def request(self, method: str, url: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Optional[httpx.Response]:  # type: ignore[override]
        reply = None
        try:
            with statsd.timed(  # type: ignore[no-untyped-call]
                "http.client.request.time", tags=[f"hostname:{self.base_url.host}"]
            ):
                reply = await super().request(method, url, *args, **kwargs)
        except http.HTTPClientSideError as e:
            if e.status_code == 403:
                _check_rate_limit(e.response)
            raise
        finally:
            if reply is None:
                status_code = "error"
            else:
                status_code = str(reply.status_code)
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

    def set_requests_ratio(self, ratio: int) -> None:
        self._requests_ratio = ratio

    def _generate_metrics(self):
        nb_requests = len(self._requests)
        statsd.histogram(
            "http.client.session",
            nb_requests,
            tags=[f"hostname:{self.base_url.host}"],
        )
        if (
            (nb_requests / self._requests_ratio) >= LOGGING_REQUESTS_THRESHOLD
            or nb_requests >= LOGGING_REQUESTS_THRESHOLD_ABSOLUTE
        ):
            LOG.warning(
                "number of GitHub requests for this session crossed the threshold (%s/%s): %s/%s",
                LOGGING_REQUESTS_THRESHOLD,
                LOGGING_REQUESTS_THRESHOLD_ABSOLUTE,
                nb_requests / self._requests_ratio,
                nb_requests,
                gh_owner=self.auth.owner,
                requests=self._requests,
                requests_ratio=self._requests_ratio,
            )
        self._requests = []


def aget_client(
    owner_name: typing.Optional[github_types.GitHubLogin] = None,
    owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
    auth: typing.Optional[_T_get_auth] = None,
) -> AsyncGithubInstallationClient:
    return AsyncGithubInstallationClient(
        auth or get_auth(owner_name=owner_name, owner_id=owner_id)
    )
