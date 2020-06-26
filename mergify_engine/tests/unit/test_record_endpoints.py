#
# Copyright © 2020 Mehdi Abaakouk <sileht@mergify.io>
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

import os
import uuid

from starlette import testclient

from mergify_engine import utils
from mergify_engine import web


def test_app_event_testing():
    with open(
        os.path.join(os.path.dirname(__file__), "events", "push_event.json"), "rb"
    ) as f:
        data = f.read()

    headers = {
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-GitHub-Event": "push",
        "X-Hub-Signature": "sha1=%s" % utils.compute_hmac(data),
        "User-Agent": "GitHub-Hookshot/044aadd",
        "Content-Type": "application/json",
    }
    with testclient.TestClient(web.app) as client:
        client.delete("/events-testing", data=data, headers=headers)
        client.post("/events-testing", data=data, headers=headers)
        client.post("/events-testing", data=data, headers=headers)
        client.post("/events-testing", data=data, headers=headers)
        client.post("/events-testing", data=data, headers=headers)
        client.post("/events-testing", data=data, headers=headers)
        events = client.get(
            "/events-testing?number=3", data=data, headers=headers
        ).json()
        assert 3 == len(events)
        events = client.get("/events-testing", data=data, headers=headers).json()
        assert 2 == len(events)
        events = client.get("/events-testing", data=data, headers=headers).json()
        assert 0 == len(events)
        client.post("/events-testing", data=data, headers=headers)
        client.delete("/events-testing", data=data, headers=headers)
        events = client.get("/events-testing", data=data, headers=headers).json()
        assert 0 == len(events)
