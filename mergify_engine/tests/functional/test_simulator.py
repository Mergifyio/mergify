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


class TestSimulator(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    async def test_simulator_with_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.master_branch_name}
      - or:
        - schedule=MON-SUN 00:00-23:59
        - label=foobar
    actions:
      assign:
        users:
          - mergify-test1
"""

        r = await self.app.post(
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200, r.json()
        assert r.json()["title"] == "The configuration is valid"
        assert r.json()["summary"] == ""

        r = await self.app.post(
            "/simulator/",
            json={"pull_request": p["html_url"], "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )

        assert r.json()["title"] == "1 rule matches"
        assert r.json()["summary"].startswith(
            f"### Rule: assign (assign)\n- [X] `base={self.master_branch_name}`\n- [X] any of:\n  - [X] `schedule=MON-SUN 00:00-23:59`\n  - [ ] `label=foobar`\n\n<hr />"
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
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400, r.json()
        assert r.json() == {
            "errors": [
                "expected str @ pull_request_rules → item 0 → actions → label → remove → item 0",
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
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400, r.json()
        assert r.json() == {
            "errors": [
                "expected str @ pull_request_rules → item 0 → actions → label → remove → item 0",
                "extra keys not allowed @ pull_request_rules → item 0 → conditions → item 0 → -conflict",
            ]
        }

    async def test_simulator_with_wrong_pull_request_url(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.master_branch_name}
    actions:
      assign:
        users:
          - mergify-test1
"""
        mock_pr_url = f"{p['html_url']}424242"
        r = await self.app.post(
            "/simulator/",
            json={"pull_request": mock_pr_url, "mergify.yml": mergify_yaml},
            headers={
                "Authorization": f"token {config.EXTERNAL_USER_PERSONAL_TOKEN}",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400, r.json()
        assert r.json() == {
            "errors": [
                f"Pull request {p['base']['repo']['full_name']}/pulls/{p['number']}424242 not found"
            ]
        }

    async def test_simulator_with_signature(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        mergify_yaml = f"""pull_request_rules:
  - name: assign
    conditions:
      - base={self.master_branch_name}
    actions:
      assign:
        users:
          - mergify-test1
"""

        r = await self.app.post(
            "/simulator/",
            json={"pull_request": None, "mergify.yml": mergify_yaml},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 200, r.json()
        assert r.json()["title"] == "The configuration is valid"
        assert r.json()["summary"] == ""

        r = await self.app.post(
            "/simulator/",
            json={"pull_request": p["html_url"], "mergify.yml": mergify_yaml},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )

        assert r.json()["title"] == "1 rule matches"
        assert r.json()["summary"].startswith(
            f"### Rule: assign (assign)\n- [X] `base={self.master_branch_name}`\n\n<hr />"
        )

        r = await self.app.post(
            "/simulator/",
            json={"pull_request": p["html_url"], "mergify.yml": "- no\n* way"},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400
        assert r.json() == {
            "errors": [
                """Invalid YAML @ line 2, column 2
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
            ],
        }

        r = await self.app.post(
            "/simulator/",
            json={"invalid": "json"},
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.status_code == 400
        assert r.json() == {
            "errors": [
                "extra keys not allowed @ invalid",
                "required key not provided",
                "required key not provided @ pull_request",
            ],
        }
