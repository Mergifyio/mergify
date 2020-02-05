# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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

from starlette import testclient

from mergify_engine import web


def test_badge_redirect():
    with testclient.TestClient(web.app) as client:
        reply = client.get(
            "/badges/mergifyio/mergify-engine.png", allow_redirects=False
        )
        assert reply.status_code == 302
        assert reply.headers["Location"] == (
            "https://img.shields.io/endpoint.png"
            "?url=https://dashboard.mergify.io/badges/mergifyio/mergify-engine&style=flat"
        )

    with testclient.TestClient(web.app) as client:
        reply = client.get(
            "/badges/mergifyio/mergify-engine.svg", allow_redirects=False
        )
        assert reply.status_code == 302
        assert reply.headers["Location"] == (
            "https://img.shields.io/endpoint.svg"
            "?url=https://dashboard.mergify.io/badges/mergifyio/mergify-engine&style=flat"
        )


def test_badge_endpoint():
    with testclient.TestClient(web.app) as client:
        reply = client.get("/badges/mergifyio/mergify-engine", allow_redirects=False)
        assert reply.headers["Location"] == (
            "https://dashboard.mergify.io/badges/mergifyio/mergify-engine"
        )
