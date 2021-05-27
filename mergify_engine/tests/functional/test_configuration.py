# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
from unittest import mock

import yaml

from mergify_engine import context
from mergify_engine.tests.functional import base


class TestConfiguration(base.FunctionalTestBase):
    async def test_invalid_configuration_in_repository(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "wrong key": 123,
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        assert check["output"]["summary"] == (
            "* extra keys not allowed @ pull_request_rules → item 0 → wrong key\n"
            "* required key not provided @ pull_request_rules → item 0 → actions\n"
            "* required key not provided @ pull_request_rules → item 0 → conditions"
        )

    async def test_invalid_yaml_configuration_in_repository(self):
        await self.setup_repo("- this is totally invalid yaml\\n\n  - *\n*")
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        # Use startswith because the message has some weird \x00 char
        assert check["output"]["summary"].startswith(
            """Invalid YAML @ line 3, column 2
```
while scanning an alias
  in "<byte string>", line 3, column 1:
    *
    ^
expected alphabetic or numeric character, but found"""
        )
        check_id = check["id"]
        annotations = [
            annotation
            async for annotation in ctxt.client.items(
                f"{ctxt.base_url}/check-runs/{check_id}/annotations",
                api_version="antiope",
            )
        ]
        assert annotations == [
            {
                "path": ".mergify.yml",
                "blob_href": mock.ANY,
                "start_line": 3,
                "start_column": 2,
                "end_line": 3,
                "end_column": 2,
                "annotation_level": "failure",
                "title": "Invalid YAML",
                "message": mock.ANY,
                "raw_details": None,
            }
        ]

    async def test_invalid_configuration_in_pull_request(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["base=master"],
                    "actions": {
                        "comment": {"message": "hello"},
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr(files={".mergify.yml": "not valid"})

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        assert (
            checks[0]["output"]["title"]
            == "Configuration changed. This pull request must be merged manually — no rules match, no planned actions"
        )
        assert (
            checks[1]["output"]["title"] == "The new Mergify configuration is invalid"
        )
        assert checks[1]["output"]["summary"] == "expected a dictionary"

    async def test_change_mergify_yml(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p1, commits1 = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert (
            checks[0]["output"]["title"]
            == "Configuration changed. This pull request must be merged manually — no rules match, no planned actions"
        )
