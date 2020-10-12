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
from unittest import mock

import pytest
from werkzeug.http import http_date
from werkzeug.wrappers import Response

from mergify_engine import exceptions
from mergify_engine.clients import github
from mergify_engine.clients import http


@mock.patch.object(github.CachedToken, "STORAGE", {})
def test_client_installation_token(httpserver):
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
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
            {"token": "<app_token>", "expires_at": "2100-12-31T23:59:59Z"}
        )

        httpserver.expect_request(
            "/", headers={"Authorization": "token <installation-token>"}
        ).respond_with_json({"work": True}, status=200)

        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            ret = client.get(httpserver.url_for("/"))
            assert ret.json()["work"]

    assert len(httpserver.log) == 3

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
def test_client_user_token(httpserver):
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        httpserver.expect_request("/users/owner").respond_with_json(
            {
                "login": "testing",
                "id": 12345,
            }
        )
        httpserver.expect_request(
            "/", headers={"Authorization": "token <user-token>"}
        ).respond_with_json({"work": True}, status=200)

        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            ret = client.get(httpserver.url_for("/"), oauth_token="<user-token>")
            assert ret.json()["work"]

    assert len(httpserver.log) == 2


@mock.patch.object(github.CachedToken, "STORAGE", {})
def test_client_401_raise_ratelimit(httpserver):
    owner = "owner"
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
    ).respond_with_json({"token": "<token>", "expires_at": "2100-12-31T23:59:59Z"})

    httpserver.expect_oneshot_request("/rate_limit").respond_with_json(
        {"resources": {"core": {"remaining": 5000, "reset": 1234567890}}}
    )
    httpserver.expect_oneshot_request("/repos/owner/repo/pull/1").respond_with_json(
        {"message": "quota !"}, status=403
    )
    httpserver.expect_oneshot_request("/rate_limit").respond_with_json(
        {"resources": {"core": {"remaining": 0, "reset": 1234567890}}}
    )

    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        client = github.get_client(owner)
        with pytest.raises(exceptions.RateLimited):
            client.item(f"/repos/{owner}/{repo}/pull/1")

    httpserver.check_assertions()


def test_client_HTTP_400(httpserver):
    httpserver.expect_oneshot_request("/").respond_with_json(
        {"message": "This is an 4XX error"}, status=400
    )

    with http.Client() as client:
        with pytest.raises(http.HTTPClientSideError) as exc_info:
            client.get(httpserver.url_for("/"))

    assert exc_info.value.message == "This is an 4XX error"
    assert exc_info.value.status_code == 400
    assert exc_info.value.response.status_code == 400
    assert str(exc_info.value.request.url) == httpserver.url_for("/")

    httpserver.check_assertions()


def test_client_HTTP_500(httpserver):
    httpserver.expect_request("/").respond_with_data("This is an 5XX error", status=500)

    with http.Client() as client:
        with pytest.raises(http.HTTPServerSideError) as exc_info:
            client.get(httpserver.url_for("/"))

    # 5 retries
    assert len(httpserver.log) == 5

    assert exc_info.value.message == "This is an 5XX error"
    assert exc_info.value.status_code == 500
    assert exc_info.value.response.status_code == 500
    assert str(exc_info.value.request.url) == httpserver.url_for("/")

    httpserver.check_assertions()


def test_client_temporary_HTTP_500(httpserver):
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

    with http.Client() as client:
        client.get(httpserver.url_for("/"))

    # 4 retries
    assert len(httpserver.log) == 4

    httpserver.check_assertions()


def test_client_connection_error():
    with http.Client() as client:
        with pytest.raises(http.RequestError):
            client.get("http://localhost:12345")


def _do_test_client_retry_429(httpserver, retry_after, expected_seconds):
    records = []

    def record_date(_):
        records.append(datetime.datetime.utcnow())
        return Response("It works now !", 200)

    httpserver.expect_oneshot_request("/").respond_with_data(
        "This is an 429 error", status=429, headers={"Retry-After": retry_after}
    )
    httpserver.expect_request("/").respond_with_handler(record_date)

    with http.Client() as client:
        now = datetime.datetime.utcnow()
        client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 2
    elapsed_seconds = (records[0] - now).total_seconds()
    assert expected_seconds - 1 < elapsed_seconds <= expected_seconds + 1
    httpserver.check_assertions()


def test_client_retry_429_retry_after_as_seconds(httpserver):
    _do_test_client_retry_429(httpserver, 3, 3)


def test_client_retry_429_retry_after_as_absolute_date(httpserver):
    retry_after = http_date(datetime.datetime.utcnow() + datetime.timedelta(seconds=3))
    _do_test_client_retry_429(httpserver, retry_after, 3)


@mock.patch.object(github.CachedToken, "STORAGE", {})
def test_client_access_token_HTTP_500(httpserver):
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
        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            with pytest.raises(http.HTTPServerSideError) as exc_info:
                client.get(httpserver.url_for("/"))

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
def test_client_installation_HTTP_500(httpserver):
    httpserver.expect_request("/users/owner/installation").respond_with_data(
        "This is an 5XX error", status=500
    )
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            with pytest.raises(http.HTTPServerSideError) as exc_info:
                client.get(httpserver.url_for("/"))

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
def test_client_installation_HTTP_404(httpserver):
    httpserver.expect_request("/users/owner/installation").respond_with_json(
        {"message": "Repository not found"}, status=404
    )
    with mock.patch(
        "mergify_engine.config.GITHUB_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            with pytest.raises(exceptions.MergifyNotInstalled):
                client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 1

    httpserver.check_assertions()


@mock.patch.object(github.CachedToken, "STORAGE", {})
def test_client_installation_HTTP_301(httpserver):
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
        with github.GithubInstallationClient(github.get_auth("owner")) as client:
            with pytest.raises(exceptions.MergifyNotInstalled):
                client.get(httpserver.url_for("/"))

    assert len(httpserver.log) == 2

    httpserver.check_assertions()
