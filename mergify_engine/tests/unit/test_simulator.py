# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
from unittest import mock

from pytest_httpserver import httpserver
from starlette import testclient
import yaml

from mergify_engine.web import root


def test_simulator_without_pull_request(httpserver: httpserver.HTTPServer) -> None:
    with mock.patch(
        "mergify_engine.config.GITHUB_REST_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        httpserver.expect_request("/user").respond_with_json(
            {"id": 12345, "login": "testing"}
        )
        with testclient.TestClient(root.app) as client:
            yaml_config = yaml.dump(
                {
                    "pull_request_rules": [
                        {
                            "name": "Automerge",
                            "conditions": [
                                "base=main",
                                "#files>100",
                                "-#files<=100",
                            ],
                            "actions": {"merge": {}},
                        }
                    ]
                }
            )
            data = json.dumps(
                {"mergify.yml": yaml_config, "pull_request": None}
            ).encode()
            headers = {
                "Authorization": "token foobar",
                "Content-Type": "application/json",
            }
            reply = client.post("/simulator/", data=data, headers=headers)
            assert reply.status_code == 200, reply.content
            assert json.loads(reply.content) == {
                "title": "The configuration is valid",
                "summary": "",
            }


def test_simulator_with_invalid_json(
    httpserver: httpserver.HTTPServer,
) -> None:
    with mock.patch(
        "mergify_engine.config.GITHUB_REST_API_URL",
        httpserver.url_for("/")[:-1],
    ):
        httpserver.expect_request("/user").respond_with_json(
            {"id": 12345, "login": "testing"}
        )
        with testclient.TestClient(root.app) as client:
            data = "invalid:json".encode()
            headers = {
                "Authorization": "token foobar",
                "Content-Type": "application/json",
            }
            reply = client.post("/simulator/", data=data, headers=headers)
            assert reply.status_code == 400, reply.content
