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

from starlette import testclient
import yaml

from mergify_engine import web


def test_config_validator() -> None:
    with testclient.TestClient(web.app) as client:
        data = yaml.dump(
            {
                "pull_request_rules": [
                    {
                        "name": "Automerge",
                        "conditions": [
                            "base=master",
                            "#files>100",
                        ],
                        "actions": {"merge": {}},
                    }
                ]
            }
        ).encode()
        reply = client.post("/validate/", files={"data": (".mergify.yml", data)})
        assert reply.status_code == 200, reply.content
        assert reply.content == b"The configuration is valid"
