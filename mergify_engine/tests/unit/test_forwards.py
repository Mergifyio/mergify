# -*- encoding: utf-8 -*-
#
# Copyright © 2019 Mehdi Abaakouk <sileht@mergify.io>
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
from unittest import mock
import uuid

# TODO(sileht): Replace me by mock.AsyncMock when we drop py37
import asyncmock
from starlette import testclient

from mergify_engine import tasks
from mergify_engine import utils
from mergify_engine import web


# NOTE(sileht): Celery magic, this just skip amqp and execute tasks directly
# So all REST API calls will block and execute celery tasks directly
tasks.app.conf.task_always_eager = True
tasks.app.conf.task_eager_propagates = True


@mock.patch(
    "mergify_engine.github_events.job_filter_and_dispatch",
    new_callable=asyncmock.AsyncMock,
)
@mock.patch(
    "mergify_engine.config.WEBHOOK_FORWARD_EVENT_TYPES",
    new_callable=mock.PropertyMock(return_value=["push"]),
)
def test_app_event_forward(_, __, httpserver):

    with open(os.path.dirname(__file__) + "/push_event.json", "r") as f:
        data = f.read()

    headers = {
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-GitHub-Event": "push",
        "X-Hub-Signature": "sha1=%s" % utils.compute_hmac(data.encode()),
        "User-Agent": "GitHub-Hookshot/044aadd",
        "Content-Type": "application/json",
    }
    httpserver.expect_request(
        "/", method="POST", data=data, headers=headers
    ).respond_with_data("")

    with mock.patch(
        "mergify_engine.config.WEBHOOK_APP_FORWARD_URL", httpserver.url_for("/"),
    ):
        with testclient.TestClient(web.app) as client:
            client.post("/event", data=data, headers=headers)

    httpserver.check_assertions()


@mock.patch("mergify_engine.web.sync_job_marketplace")
@mock.patch(
    "mergify_engine.config.WEBHOOK_FORWARD_EVENT_TYPES",
    new_callable=mock.PropertyMock(return_value=["purchased"]),
)
def test_market_event_forward(_, __, httpserver):

    with open(os.path.dirname(__file__) + "/market_event.json", "r") as f:
        data = f.read()

    headers = {
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-GitHub-Event": "purchased",
        "X-Hub-Signature": "sha1=%s" % utils.compute_hmac(data.encode()),
        "User-Agent": "GitHub-Hookshot/044aadd",
        "Content-Type": "application/json",
    }
    httpserver.expect_request(
        "/", method="POST", data=data, headers=headers
    ).respond_with_data("")

    with mock.patch(
        "mergify_engine.config.WEBHOOK_MARKETPLACE_FORWARD_URL",
        httpserver.url_for("/"),
    ):
        with testclient.TestClient(web.app) as client:
            client.post("/marketplace", data=data, headers=headers)

    httpserver.check_assertions()
