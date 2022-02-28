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
import json
import os
from unittest import mock

import pytest
from starlette import testclient

from mergify_engine import config
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.web import root


with open(os.path.join(os.path.dirname(__file__), "events", "push.json")) as f:
    push_event = json.load(f)

with open(os.path.join(os.path.dirname(__file__), "events", "pull_request.json")) as f:
    pull_request_event = json.load(f)


@pytest.mark.parametrize(
    "event,event_type,status_code,reason",
    (
        (
            {
                "sender": {
                    "login": "JD",
                },
                "event_type": "foobar",
            },
            None,
            200,
            b"Event ignored: unexpected event_type",
        ),
        (
            push_event,
            "push",
            200,
            b"Event ignored: push on refs/tags/simple-tag",
        ),
        (
            pull_request_event,
            "pull_request",
            202,
            b"Event queued",
        ),
    ),
)
@mock.patch(
    "mergify_engine.config.WEBHOOK_SECRET_PRE_ROTATION",
    new_callable=mock.PropertyMock(return_value="secret!!"),
)
def test_push_event(
    _: mock.PropertyMock,
    event: github_types.GitHubEvent,
    event_type: str,
    status_code: int,
    reason: bytes,
) -> None:
    with testclient.TestClient(root.app) as client:
        charset = "utf-8"
        data = json.dumps(event).encode(charset)
        headers = {
            "X-Hub-Signature": f"sha1={utils.compute_hmac(data, config.WEBHOOK_SECRET)}",
            "X-GitHub-Event": event_type,
            "Content-Type": f"application/json; charset={charset}",
        }
        reply = client.post(
            "/event",
            data=data,
            headers=headers,
        )
        assert reply.content == reason
        assert reply.status_code == status_code

        # Same with WEBHOOK_SECRET_PRE_ROTATION for key rotation
        assert config.WEBHOOK_SECRET_PRE_ROTATION is not None
        charset = "utf-8"
        data = json.dumps(event).encode(charset)
        headers = {
            "X-Hub-Signature": f"sha1={utils.compute_hmac(data, config.WEBHOOK_SECRET_PRE_ROTATION)}",
            "X-GitHub-Event": event_type,
            "Content-Type": f"application/json; charset={charset}",
        }
        reply = client.post(
            "/event",
            data=data,
            headers=headers,
        )
        assert reply.content == reason
        assert reply.status_code == status_code
