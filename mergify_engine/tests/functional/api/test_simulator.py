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
import yaml

from mergify_engine import config
from mergify_engine.tests.functional import base


class TestApiSimulator(base.FunctionalTestBase):
    async def test_simulator_with_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.main_branch_name}
      - or:
        - schedule=MON-SUN 00:00-23:59
        - label=foobar
    actions:
      assign:
        users:
          - mergify-test1
"""

        r = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/simulator",
            json={"mergify_yml": mergify_yaml},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert r.status_code == 200, r.json()
        assert r.json()["title"] == "The configuration is valid"
        assert r.json()["summary"] == ""

        r = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/simulator",
            json={"mergify_yml": mergify_yaml},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )

        assert r.json()["title"] == "1 rule matches"
        assert r.json()["summary"].startswith(
            f"### Rule: assign (assign)\n- [X] `base={self.main_branch_name}`\n- [X] any of:\n  - [X] `schedule=MON-SUN 00:00-23:59`\n  - [ ] `label=foobar`\n\n<hr />"
        ), r.json()["summary"]

        mergify_yaml = """pull_request_rules:
  - name: remove label conflict
    conditions:
      - -conflict
    actions:
      label:
        remove:
          - conflict:
"""

        r = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/simulator",
            json={"mergify_yml": mergify_yaml},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert r.status_code == 422, r.json()
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "mergify_yml"],
                    "msg": "expected str @ pull_request_rules → item 0 → actions → label → remove → item 0",
                    "type": "mergify_config_error",
                }
            ]
        }

        mergify_yaml = """pull_request_rules:
  - name: remove label conflict
    conditions:
      - -conflict:
    actions:
      label:
        remove:
          - conflict:
"""

        r = await self.app.post(
            f"/v1/repos/{p['base']['repo']['owner']['login']}/{p['base']['repo']['name']}/simulator",
            json={"mergify_yml": mergify_yaml},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert r.status_code == 422, r.json()
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "mergify_yml"],
                    "msg": "expected str @ pull_request_rules → item 0 → actions → label → remove → item 0",
                    "type": "mergify_config_error",
                },
                {
                    "loc": ["body", "mergify_yml"],
                    "msg": "extra keys not allowed @ pull_request_rules → item 0 → conditions → item 0 → -conflict",
                    "type": "mergify_config_error",
                },
            ]
        }

    async def test_simulator_with_wrong_pull_request_url(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.main_branch_name}
    actions:
      assign:
        users:
          - mergify-test1
"""
        resp = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/42424242/simulator",
            json={"mergify_yml": mergify_yaml},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert resp.status_code == 404

    async def test_simulator_invalid_json(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()

        r = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/pulls/{p['number']}/simulator",
            json={"mergify_yml": "- no\n* way"},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert r.status_code == 422
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "mergify_yml"],
                    "msg": """Invalid YAML @ line 2, column 2
```
while scanning an alias
  in "<unicode string>", line 2, column 1:
    * way
    ^
expected alphabetic or numeric character, but found ' '
  in "<unicode string>", line 2, column 2:
    * way
     ^
```""",
                    "type": "mergify_config_error",
                }
            ],
        }

        r = await self.app.post(
            f"/v1/repos/{config.TESTING_ORGANIZATION_NAME}/{self.RECORD_CONFIG['repository_name']}/simulator",
            json={"invalid": "json"},
            headers={
                "Authorization": f"bearer {self.api_key_admin}",
            },
        )
        assert r.status_code == 422
        assert r.json() == {
            "detail": [
                {
                    "loc": ["body", "mergify_yml"],
                    "msg": "field required",
                    "type": "value_error.missing",
                },
            ],
        }
