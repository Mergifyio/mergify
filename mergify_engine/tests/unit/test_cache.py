# -*- encoding: utf-8 -*-
#
# Copyright © 2019–2020 Julien Danjou <jd@mergify.io>
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
import json

from starlette import testclient

from mergify_engine import utils
from mergify_engine import web


def test_subscription_cache_delete():
    installation_id = 123

    data = None
    headers = {
        "X-Hub-Signature": "sha1=%s" % utils.compute_hmac(data),
    }
    with testclient.TestClient(web.app) as client:
        reply = client.delete(
            f"/subscription-cache/{installation_id}", data=data, headers=headers
        )
        assert reply.status_code == 200
        assert reply.content == b"Cache cleaned"


def test_subscription_cache_update():
    installation_id = 123
    charset = "utf-8"

    data = json.dumps(
        {
            "subscription_active": True,
            "subscription_reason": "Customer",
            "tokens": {},
            "features": [],
        }
    ).encode(charset)
    headers = {
        "X-Hub-Signature": "sha1=%s" % utils.compute_hmac(data),
        "Content-Type": f"application/json; charset={charset}",
    }
    with testclient.TestClient(web.app) as client:
        reply = client.put(
            f"/subscription-cache/{installation_id}", data=data, headers=headers
        )
        assert reply.status_code == 200
        assert reply.content == b"Cache updated"
