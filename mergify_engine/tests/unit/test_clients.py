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

import datetime
import typing
from unittest import mock

import pytest
from pytest_httpserver import httpserver
from werkzeug.http import http_date
from werkzeug.wrappers import Response

from mergify_engine import date
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine.clients import github
from mergify_engine.clients import http


async def _do_test_client_installation_token(
    httpserver: httpserver.HTTPServer,
    endpoint: str,
    owner_name: typing.Optional[github_types.GitHubLogin] = None,
    owner_id: typing.Optional[github_types.GitHubAccountIdType] = None,
) -> None:

    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):

        httpserver.expect_request(endpoint).respond_with_json(
            {
                "id": 12345,
                "target_type": "User",
                "permissions": {
                    "checks": "write",
                    "contents": "write",
                    "pull_requests": "write",
                },
                "account": {"login": "testing", "id": 12345},
            }
        )

        httpserver.expect_request(
            "/app/installations/12345/access_tokens"
        ).respond_with_json(
            {"token": "<app_token>", "expires_at": "2100-12-31T23:59:59Z"}
        )

        httpserver.expect_request(
            "/", headers={"Authorization": "token <installation-token>"}
        ).respond_with_json({"work": True}, status=200)

        async with github.AsyncGithubInstallationClient(
            github.get_auth(owner_name=owner_name, owner_id=owner_id)
        ) as client:

            ret = await client.get(httpserver.url_for("/"))
            assert ret.json()["work"]
            assert client.auth.owner == "testing"
            assert client.auth.owner_id == 12345

    assert len(httpserver.log) == 3

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_installation_token_with_owner_name(
    httpserver: httpserver.HTTPServer,
) -> None:
    await _do_test_client_installation_token(
        httpserver,
        "/users/owner/installation",
        owner_name=github_types.GitHubLogin("owner"),
    )


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_installation_token_with_owner_id(
    httpserver: httpserver.HTTPServer,
) -> None:
    await _do_test_client_installation_token(
        httpserver,
        "/user/12345/installation",
        owner_id=github_types.GitHubAccountIdType(12345),
    )


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_user_token(httpserver: httpserver.HTTPServer) -> None:
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        httpserver.expect_request("/user").respond_with_json(
            {
                "login": "testing",
                "id": 12345,
            }
        )
        httpserver.expect_request(
            "/", headers={"Authorization": "token <user-token>"}
        ).respond_with_json({"work": True}, status=200)

        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            ret = await client.get(
                httpserver.url_for("/"),
                oauth_token=github_types.GitHubOAuthToken("<user-token>"),
            )
            assert ret.json()["work"]

    assert len(httpserver.log) == 2


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_401_raise_ratelimit(httpserver: httpserver.HTTPServer) -> None:
    owner = github_types.GitHubLogin("owner")
    repo = "repo"

    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {
            "id": 12345,
            "target_type": "User",
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "account": {"login": "testing", "id": 12345},
        }
    )
    httpserver.expect_request(
        "/app/installations/12345/access_tokens"
    ).respond_with_json(
        {"token": "<token>", "expires_at": "2100-12-31T23:59:59Z"},
        headers={"X-RateLimit-Remaining": 5000, "X-RateLimit-Reset": 1234567890},
    )

    httpserver.expect_oneshot_request("/repos/owner/repo/pull/1").respond_with_json(
        {"message": "quota !"},
        status=403,
        headers={"X-RateLimit-Remaining": 0, "X-RateLimit-Reset": 1234567890},
    )

    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.aget_client(owner) as client:
            with pytest.raises(exceptions.RateLimited):
                await client.item(f"/repos/{owner}/{repo}/pull/1")

    httpserver.check_assertions()


@pytest.mark.asyncio
async def test_client_HTTP_400(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_oneshot_request("/").respond_with_json(
        {"message": "This is an 4XX error"}, status=400
    )

    async with http.AsyncClient() as client:
        with pytest.raises(http.HTTPClientSideError) as exc_info:
            await client.get(httpserver.url_for("/"))

    assert exc_info.value.message == "This is an 4XX error"
    assert exc_info.value.status_code == 400
    assert exc_info.value.response.status_code == 400
    assert str(exc_info.value.request.url) == httpserver.url_for("/")

    httpserver.check_assertions()


@pytest.mark.asyncio
async def test_client_HTTP_500(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data("This is an 5XX error", status=500)

    async with http.AsyncClient() as client:
        with pytest.raises(http.HTTPServerSideError) as exc_info:
            await client.get(httpserver.url_for("/"))

    # 5 retries
    assert len(httpserver.log) == 5

    assert exc_info.value.message == "This is an 5XX error"
    assert exc_info.value.status_code == 500
    assert exc_info.value.response.status_code == 500
    assert str(exc_info.value.request.url) == httpserver.url_for("/")

    httpserver.check_assertions()


@pytest.mark.asyncio
async def test_client_temporary_HTTP_500(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_oneshot_request("/").respond_with_data(
        "This is an 5XX error", status=500
    )
    httpserver.expect_oneshot_request("/").respond_with_data(
        "This is an 5XX error", status=500
    )
    httpserver.expect_oneshot_request("/").respond_with_data(
        "This is an 5XX error", status=500
    )
    httpserver.expect_request("/").respond_with_data("It works now !", status=200)

    async with http.AsyncClient() as client:
        await client.get(httpserver.url_for("/"))

    # 4 retries
    assert len(httpserver.log) == 4

    httpserver.check_assertions()


@pytest.mark.asyncio
async def test_client_connection_error() -> None:
    async with http.AsyncClient() as client:
        with pytest.raises(http.RequestError):
            await client.get("http://localhost:12345")


async def _do_test_client_retry_429(
    httpserver: httpserver.HTTPServer, retry_after: str, expected_seconds: int
) -> None:
    records = []

    def record_date(_):
        records.append(date.utcnow())
        return Response("It works now !", 200)

    httpserver.expect_oneshot_request("/").respond_with_data(
        "This is an 429 error", status=429, headers={"Retry-After": retry_after}
    )
    httpserver.expect_request("/").respond_with_handler(record_date)

    async with http.AsyncClient() as client:
        now = date.utcnow()
        await client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 2
    elapsed_seconds = (records[0] - now).total_seconds()
    assert expected_seconds - 1 < elapsed_seconds <= expected_seconds + 1
    httpserver.check_assertions()


@pytest.mark.asyncio
async def test_client_retry_429_retry_after_as_seconds(
    httpserver: httpserver.HTTPServer,
) -> None:
    await _do_test_client_retry_429(httpserver, "3", 3)


@pytest.mark.asyncio
async def test_client_retry_429_retry_after_as_absolute_date(
    httpserver: httpserver.HTTPServer,
) -> None:
    retry_after = http_date(date.utcnow() + datetime.timedelta(seconds=3))
    await _do_test_client_retry_429(httpserver, retry_after, 3)


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_access_token_HTTP_500(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {
            "id": 12345,
            "target_type": "User",
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "account": {"login": "testing", "id": 12345},
        }
    )
    httpserver.expect_request(
        "/app/installations/12345/access_tokens"
    ).respond_with_data("This is an 5XX error", status=500)

    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            with pytest.raises(http.HTTPServerSideError) as exc_info:
                await client.get(httpserver.url_for("/"))

    # installation request + 5 retries
    assert len(httpserver.log) == 6

    assert exc_info.value.message == "This is an 5XX error"
    assert exc_info.value.status_code == 500
    assert exc_info.value.response.status_code == 500
    assert str(exc_info.value.request.url) == httpserver.url_for(
        "/app/installations/12345/access_tokens"
    )

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_installation_HTTP_500(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request("/users/owner/installation").respond_with_data(
        "This is an 5XX error", status=500
    )
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            with pytest.raises(http.HTTPServerSideError) as exc_info:
                await client.get(httpserver.url_for("/"))

    # 5 retries
    assert len(httpserver.log) == 5

    assert exc_info.value.message == "This is an 5XX error"
    assert exc_info.value.status_code == 500
    assert exc_info.value.response.status_code == 500
    assert str(exc_info.value.request.url) == httpserver.url_for(
        "/users/owner/installation"
    )

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_installation_HTTP_404(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {"message": "Repository not found"}, status=404
    )
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            with pytest.raises(exceptions.MergifyNotInstalled):
                await client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 1

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_installation_HTTP_301(httpserver: httpserver.HTTPServer) -> None:
    httpserver.expect_request("/users/owner/installation").respond_with_data(
        status=301,
        headers={"Location": httpserver.url_for("/repositories/12345/installation")},
    )
    httpserver.expect_request("/repositories/12345/installation").respond_with_json(
        {"message": "Repository not found"}, status=404
    )
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            with pytest.raises(exceptions.MergifyNotInstalled):
                await client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 2

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
@pytest.mark.asyncio
async def test_client_abuse_403_no_header(httpserver: httpserver.HTTPServer) -> None:

    abuse_message = (
        "You have triggered an abuse detection mechanism. "
        "Please wait a few minutes before you try again."
    )
    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {
            "id": 12345,
            "target_type": "User",
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "account": {"login": "testing", "id": 12345},
        }
    )
    httpserver.expect_request(
        "/app/installations/12345/access_tokens"
    ).respond_with_json({"token": "<token>", "expires_at": "2100-12-31T23:59:59Z"})

    httpserver.expect_oneshot_request("/").respond_with_json(
        {"message": abuse_message},
        status=403,
    )

    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        async with github.AsyncGithubInstallationClient(
            github.get_auth(github_types.GitHubLogin("owner"))
        ) as client:
            with pytest.raises(http.HTTPClientSideError) as exc_info:
                await client.get(httpserver.url_for("/"))

    assert exc_info.value.message == abuse_message
    assert exc_info.value.status_code == 403
    assert exc_info.value.response.status_code == 403
    assert str(exc_info.value.request.url) == httpserver.url_for("/")
    assert len(httpserver.log) == 3

    httpserver.check_assertions()
