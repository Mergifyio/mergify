# -*- encoding: utf-8 -*-
#
# Copyright © 2019–2020 Mergify SAS
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

import operator

import yaml

from mergify_engine import config
from mergify_engine.tests.functional import base


class TestSimulator(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def test_simulator_with_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.master_branch_name}
    actions:
      assign:
        users:
          - mergify-test1
"""

        r = self.app.post(
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200, r.json()
        assert r.json()["title"] == "The configuration is valid"
        assert r.json()["summary"] is None

        r = self.app.post(
            "/simulator/",
            json={"pull_request": p.html_url, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )

        assert r.json()["title"] == "1 rule matches"
        assert r.json()["summary"].startswith(
            f"#### Rule: assign (assign)\n- [X] `base={self.master_branch_name}`\n\n<hr />"
        )

    def test_simulator_with_signature(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.master_branch_name}
    actions:
      assign:
        users:
          - mergify-test1
"""

        r = self.app.post(
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200, r.json()
        assert r.json()["title"] == "The configuration is valid"
        assert r.json()["summary"] is None

        r = self.app.post(
            "/simulator/",
            json={"pull_request": p.html_url, "mergify.yml": mergify_yaml},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )

        assert r.json()["title"] == "1 rule matches"
        assert r.json()["summary"].startswith(
            f"#### Rule: assign (assign)\n- [X] `base={self.master_branch_name}`\n\n<hr />"
        )

        r = self.app.post(
            "/simulator/",
            json={"pull_request": p.html_url, "mergify.yml": "- no\n* way"},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400
        assert r.json() == {
            "type": "MultipleInvalid",
            "message": """while scanning an alias
  in "<unicode string>", line 2, column 1:
    * way
    ^
expected alphabetic or numeric character, but found ' '
  in "<unicode string>", line 2, column 2:
    * way
     ^""",
            "details": ["mergify.yml", "line 2, column 2"],
            "error": "Invalid YAML at ['mergify.yml', line 2, column 2]",
            "errors": [
                {
                    "type": "YAMLInvalid",
                    "details": ["mergify.yml", "line 2, column 2"],
                    "error": "Invalid YAML at ['mergify.yml', line 2, column 2]",
                    "message": """while scanning an alias
  in "<unicode string>", line 2, column 1:
    * way
    ^
expected alphabetic or numeric character, but found ' '
  in "<unicode string>", line 2, column 2:
    * way
     ^""",
                }
            ],
        }

        r = self.app.post(
            "/simulator/",
            json={"invalid": "json"},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400
        r.json()["errors"] = sorted(
            r.json()["errors"], key=operator.itemgetter("message")
        )
        assert r.json() == {
            "type": "MultipleInvalid",
            "error": "extra keys not allowed @ data['invalid']",
            "details": ["invalid"],
            "message": "extra keys not allowed",
            "errors": [
                {
                    "details": ["invalid"],
                    "error": "extra keys not allowed @ data['invalid']",
                    "message": "extra keys not allowed",
                    "type": "Invalid",
                },
                {
                    "details": ["mergify.yml"],
                    "error": "required key not provided @ data['mergify.yml']",
                    "message": "required key not provided",
                    "type": "RequiredFieldInvalid",
                },
                {
                    "details": ["pull_request"],
                    "error": "required key not provided @ data['pull_request']",
                    "message": "required key not provided",
                    "type": "RequiredFieldInvalid",
                },
            ],
        }
