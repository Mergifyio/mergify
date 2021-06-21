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

from starlette import testclient
import yaml

from mergify_engine import utils
from mergify_engine.web import root


def test_simulator_without_pull_request() -> None:
    with testclient.TestClient(root.app) as client:
        yaml_config = yaml.dump(
            {
                "pull_request_rules": [
                    {
                        "name": "Automerge",
                        "conditions": [
                            "base=master",
                            "#files>100",
                            "-#files<=100",
                        ],
                        "actions": {"merge": {}},
                    }
                ]
            }
        )
        charset = "utf-8"
        data = json.dumps({"mergify.yml": yaml_config, "pull_request": None}).encode(
            charset
        )
        headers = {
            "X-Hub-Signature": f"sha1={utils.compute_hmac(data)}",
            "Content-Type": f"application/json; charset={charset}",
        }
        reply = client.post("/simulator/", data=data, headers=headers)
        assert reply.status_code == 200, reply.content
        assert json.loads(reply.content) == {
            "title": "The configuration is valid",
            "summary": "",
        }


def test_simulator_with_invalid_json() -> None:
    with testclient.TestClient(root.app) as client:
        charset = "utf-8"
        data = "invalid:json".encode(charset)
        headers = {
            "X-Hub-Signature": f"sha1={utils.compute_hmac(data)}",
            "Content-Type": f"application/json; charset={charset}",
        }
        reply = client.post("/simulator/", data=data, headers=headers)
        assert reply.status_code == 400, reply.content
