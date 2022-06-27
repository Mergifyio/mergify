# -*- encoding: utf-8 -*-
#
# Copyright © 2020—2022 Mergify SAS
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
import types
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
class TooManyPages(exceptions.UnprocessablePullRequest):
    reason: str
    per_page: int
    page_limit: int
    last_page: int


@dataclasses.dataclass
class GraphqlError(Exception):
    message: str


@dataclasses.dataclass
class CachedToken:
    STORAGE: typing.ClassVar[
        typing.Dict[github_types.GitHubInstallationIdType, "CachedToken"]
    ] = {}

    installation_id: github_types.GitHubInstallationIdType
    token: str
    expiration: datetime.datetime

    def __post_init__(self) -> None:
        CachedToken.STORAGE[self.installation_id] = self

    @classmethod
    def get(
        cls, installation_id: github_types.GitHubInstallationIdType
    ) -> typing.Optional["CachedToken"]:
        return cls.STORAGE.get(installation_id)

    def invalidate(self) -> None:
        CachedToken.STORAGE.pop(self.installation_id, None)


class InstallationInaccessible(Exception):
    message: str


class GithubTokenAuth(httpx.Auth):
    _token: str

    def __init__(self, token: str) -> None:
        self._token = token

    @contextlib.contextmanager
    def response_body_read(self) -> typing.Generator[None, None, None]:
        self.requires_response_body = True
        try:
            yield
        finally:
            self.requires_response_body = False

    def auth_flow(
        self, request: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"token {self._token}"
        yield request

    def build_request(self, method: str, url: str) -> httpx.Request:
        headers = http.DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"token {self._token}"
        return httpx.Request(method, url, headers=headers)

    def get_access_token(self) -> str:
        return self._token


class GithubAppInstallationAuth(httpx.Auth):
    installation: github_types.GitHubInstallation

    def __init__(self, installation: github_types.GitHubInstallation) -> None:
        self._installation = installation
        self._cached_token = CachedToken.get(self._installation["id"])

    @property
    def _owner_id(self) -> int:
        return self._installation["account"]["id"]

    @property
    def _owner_login(self) -> str:
        return self._installation["account"]["login"]

    @contextlib.contextmanager
    def response_body_read(self) -> typing.Generator[None, None, None]:
        self.requires_response_body = True
        try:
            yield
        finally:
            self.requires_response_body = False

    def auth_flow(
        self, request: httpx.Request
    ) -> typing.Generator[httpx.Request, httpx.Response, None]:
        token = self.get_access_token()
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
                        gh_owner=self._owner_login,
                        error_message=error_message,
                    )
                    raise exceptions.MergifyNotInstalled()

            http.raise_for_status(auth_response)
            token = self._set_access_token(auth_response.json())

        request.headers["Authorization"] = f"token {token}"
        yield request

    def build_access_token_request(self, force: bool = False) -> httpx.Request:
        if self._installation is None:
            raise RuntimeError("No installation")

        return self.build_github_app_request(
            "POST",
            f"{config.GITHUB_REST_API_URL}/app/installations/{self._installation['id']}/access_tokens",
            force=force,
        )

    def build_github_app_request(
        self, method: str, url: str, force: bool = False
    ) -> httpx.Request:
        headers = http.DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"Bearer {github_app.get_or_create_jwt(force)}"
        return httpx.Request(method, url, headers=headers)

    def _set_access_token(
        self, data: github_types.GitHubInstallationAccessToken
    ) -> str:
        if self._installation is None:
            raise RuntimeError("Cannot set access token, no installation")

        self._cached_token = CachedToken(
            self._installation["id"],
            data["token"],
            datetime.datetime.fromisoformat(data["expires_at"][:-1]),  # Remove the Z
        )
        LOG.debug(
            "New token acquired",
            gh_owner=self._owner_login,
            expire_at=self._cached_token.expiration,
        )
        return self._cached_token.token

    def get_access_token(self) -> typing.Optional[str]:
        now = datetime.datetime.utcnow()
        if not self._cached_token:
            return None
        elif self._cached_token.expiration <= now:
            LOG.debug(
                "Token expired",
                gh_owner=self._owner_login,
                expire_at=self._cached_token.expiration,
            )
            self._cached_token.invalidate()
            self._cached_token = None
            return None
        else:
            return self._cached_token.token


async def get_installation_from_account_id(
    account_id: github_types.GitHubAccountIdType,
) -> github_types.GitHubInstallation:
    async with AsyncGithubClient(auth=github_app.GithubBearerAuth()) as client:
        try:
            return typing.cast(
                github_types.GitHubInstallation,
                await client.item(
                    f"{config.GITHUB_REST_API_URL}/user/{account_id}/installation"
                ),
            )
        except http.HTTPNotFound as e:
            LOG.debug(
                "Mergify not installed",
                gh_owner_id=account_id,
                error_message=e.message,
            )
            raise exceptions.MergifyNotInstalled()


async def get_installation_from_login(
    login: github_types.GitHubLogin,
) -> github_types.GitHubInstallation:
    async with AsyncGithubClient(auth=github_app.GithubBearerAuth()) as client:
        try:
            return typing.cast(
                github_types.GitHubInstallation,
                await client.item(
                    f"{config.GITHUB_REST_API_URL}/users/{login}/installation"
                ),
            )
        except http.HTTPNotFound as e:
            LOG.debug(
                "Mergify not installed",
                gh_owner=login,
                error_message=e.message,
            )
            raise exceptions.MergifyNotInstalled()


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
            if delta < datetime.timedelta():
                # NOTE(sileht): worker minial retry is 3 sec, so no need to
                # change the delta here.
                LOG.error(
                    "got ratelimit with a reset date in the past",
                    method=response.request.method,
                    url=response.request.url,
                    final_url=response.url,
                    headers=response.headers,
                    content=response.content,
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
            kwargs["auth"] = GithubTokenAuth(oauth_token)
        return await func(url, headers=headers, **kwargs)

    return wrapper


class AsyncGithubClient(http.AsyncClient):
    auth: typing.Union[
        github_app.GithubBearerAuth, GithubAppInstallationAuth, GithubTokenAuth
    ]

    def __init__(
        self,
        auth: typing.Union[
            github_app.GithubBearerAuth, GithubAppInstallationAuth, GithubTokenAuth
        ],
    ) -> None:
        super().__init__(
            base_url=config.GITHUB_REST_API_URL,
            auth=auth,
            headers={"Accept": "application/vnd.github.machine-man-preview+json"},
        )

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
            if isinstance(self.auth, github_app.GithubBearerAuth):
                raise TypeError(
                    "oauth_token is not supported for GithubBearerAuth auth"
                )
            kwargs["auth"] = GithubTokenAuth(oauth_token)
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

    async def graphql_post(
        self,
        query: str,
        oauth_token: typing.Optional[github_types.GitHubOAuthToken] = None,
    ) -> typing.Any:
        response = await self.post(
            config.GITHUB_GRAPHQL_API_URL,
            json={"query": query},
            oauth_token=oauth_token,
        )
        data = response.json()
        if "data" not in data:
            raise GraphqlError(response.text)
        return data

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
        *,
        resource_name: str,
        page_limit: int,
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

        per_page = int(final_params["per_page"])

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
                if last_page > page_limit:
                    raise TooManyPages(
                        f"The pull request reports more than {(last_page - 1) * per_page} {resource_name}, "
                        f"reaching the limit of {page_limit * per_page} {resource_name}.",
                        per_page,
                        page_limit,
                        last_page,
                    )

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


class AsyncGithubInstallationClient(AsyncGithubClient):
    auth: typing.Union[
        GithubAppInstallationAuth,
        GithubTokenAuth,
    ]

    def __init__(
        self,
        auth: typing.Union[
            GithubAppInstallationAuth,
            GithubTokenAuth,
        ],
    ) -> None:
        self._requests: typing.List[typing.Tuple[str, str]] = []
        self._requests_ratio: int = 1
        super().__init__(auth=auth)

    async def request(self, method: str, url: str, *args: typing.Any, **kwargs: typing.Any) -> typing.Optional[httpx.Response]:  # type: ignore[override]
        reply = None
        try:
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

    async def aclose(self) -> None:
        await super().aclose()
        self._generate_metrics()

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]] = None,
        exc_value: typing.Optional[BaseException] = None,
        traceback: typing.Optional[types.TracebackType] = None,
    ) -> None:
        await super().__aexit__(exc_type, exc_value, traceback)
        self._generate_metrics()

    def set_requests_ratio(self, ratio: int) -> None:
        self._requests_ratio = ratio

    def _generate_metrics(self) -> None:
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
                gh_owner=http.extract_organization_login(self),
                requests=self._requests,
                requests_ratio=self._requests_ratio,
            )
        self._requests = []

    async def get_access_token(self) -> str:
        token = self.auth.get_access_token()
        if token:
            return token

        # Token has expired, get a new one
        await self.get("/")
        token = self.auth.get_access_token()
        if token:
            return token

        raise RuntimeError("get_access_token() call on an unused client")


def aget_client(
    installation: github_types.GitHubInstallation,
) -> AsyncGithubInstallationClient:
    return AsyncGithubInstallationClient(GithubAppInstallationAuth(installation))
