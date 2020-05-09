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

from unittest import mock

import pytest

from mergify_engine import exceptions
from mergify_engine.clients import github


@mock.patch("mergify_engine.clients.http.RETRY", None)
def test_client_401_raise_ratelimit(httpserver):
    owner = "owner"
    repo = "repo"

    httpserver.expect_request("/repos/owner/repo/installation").respond_with_json(
        {
            "id": 12345,
            "target_type": "User",
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
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
        "mergify_engine.config.GITHUB_API_URL", httpserver.url_for("/"),
    ):
        installation = github.get_installation(owner, repo, 12345)
        client = github.get_client(owner, repo, installation)
        with pytest.raises(exceptions.RateLimited):
            client.item("pull/1")
