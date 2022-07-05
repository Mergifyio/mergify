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
import typing
from unittest import mock

import pytest
import yaml

from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine.tests.functional import base


class TestConfiguration(base.FunctionalTestBase):
    async def test_invalid_configuration_fixed_by_pull_request(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "wrong key": 123,
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        rules["pull_request_rules"] = [
            {
                "name": "foobar",
                "conditions": ["label!=wip"],
                "actions": {"merge": {}},
            }
        ]
        p = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        summary_check = checks[0]
        assert (
            summary_check["output"]["title"]
            == "The current Mergify configuration is invalid"
        )
        assert summary_check["output"]["summary"] == (
            "* extra keys not allowed @ pull_request_rules → item 0 → wrong key\n"
            "* required key not provided @ pull_request_rules → item 0 → actions\n"
            "* required key not provided @ pull_request_rules → item 0 → conditions"
        )
        conf_change_check = checks[1]
        assert conf_change_check["conclusion"] == check_api.Conclusion.SUCCESS.value
        assert (
            conf_change_check["output"]["title"]
            == "The new Mergify configuration is valid"
        )

    async def test_invalid_configuration_in_repository(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "wrong key": 123,
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert (
            check["output"]["title"] == "The current Mergify configuration is invalid"
        )
        assert check["output"]["summary"] == (
            "* extra keys not allowed @ pull_request_rules → item 0 → wrong key\n"
            "* required key not provided @ pull_request_rules → item 0 → actions\n"
            "* required key not provided @ pull_request_rules → item 0 → conditions"
        )

    async def test_invalid_yaml_configuration_in_repository(self) -> None:
        await self.setup_repo("- this is totally invalid yaml\\n\n  - *\n*")
        p = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert (
            check["output"]["title"] == "The current Mergify configuration is invalid"
        )
        # Use startswith because the message has some weird \x00 char
        assert check["output"]["summary"].startswith(
            """Invalid YAML @ line 3, column 2
```
while scanning an alias
  in "<unicode string>", line 3, column 1:
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
                resource_name="annotations",
                page_limit=10,
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

    async def test_cached_config_changes_when_push_event_received(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["base=main"],
                    "actions": {
                        "comment": {"message": "hello"},
                    },
                },
            ],
        }

        rules_default: typing.Dict[str, typing.List[str]] = {"pull_request_rules": []}

        await self.setup_repo(yaml.dump(rules_default))
        assert self.git.repository is not None
        await self.repository_ctxt.get_mergify_config_file()
        await self.run_engine()

        cached_config_file = await self.repository_ctxt.get_cached_config_file(
            self.repository_ctxt.repo["id"]
        )
        assert cached_config_file is not None
        assert cached_config_file["decoded_content"] == yaml.dump(rules_default)

        # Change unrelated file and config stay cached
        with open(self.git.repository + "/random", "w") as f:
            f.write("yo")
        await self.git("add", "random")
        await self.git("commit", "--no-edit", "-m", "random update")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        cached_config_file = await self.repository_ctxt.get_cached_config_file(
            self.repository_ctxt.repo["id"]
        )
        assert cached_config_file is not None
        assert cached_config_file["decoded_content"] == yaml.dump(rules_default)

        # Change config file and config cache gets cleaned
        with open(self.git.repository + "/.mergify.yml", "w") as f:
            f.write(yaml.dump(rules))
        await self.git("add", ".mergify.yml")
        await self.git("commit", "--no-edit", "-m", "conf update")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        cached_config_file = await self.repository_ctxt.get_cached_config_file(
            self.repository_ctxt.repo["id"]
        )
        assert cached_config_file is None

        # Open a PR and it's cached again
        p = await self.create_pr()
        await context.Context.create(self.repository_ctxt, p, [])
        await self.run_engine()

        cached_config_file = await self.repository_ctxt.get_cached_config_file(
            self.repository_ctxt.repo["id"]
        )
        assert cached_config_file is not None
        assert cached_config_file["decoded_content"] == yaml.dump(rules)

    async def test_no_configuration_changed_with_weird_base_sha(self) -> None:
        # Test special case where the configuration is changed around the a
        # pull request creation.
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["base=main"],
                    "actions": {
                        "comment": {"message": "hello"},
                    },
                },
            ],
        }
        # config has been update in the meantime
        await self.setup_repo(yaml.dump({"pull_request_rules": []}))
        assert self.git.repository is not None
        with open(self.git.repository + "/.mergify.yml", "wb") as f:
            f.write(yaml.dump(rules).encode())
        await self.git("add", ".mergify.yml")
        await self.git("commit", "--no-edit", "-m", "conf update")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.run_engine()

        await self.git("branch", "save-point")
        # Create a PR on outdated repo to get a wierd base.sha
        await self.git("reset", "--hard", "HEAD^", "--")
        # Create a lot of file to ignore optimization
        p = await self.create_pr(
            git_tree_ready=True, files={f"f{i}": "data" for i in range(0, 160)}
        )
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        await self.run_engine()

        await self.git("checkout", "save-point", "-b", self.main_branch_name)
        with open(self.git.repository + "/.mergify.yml", "wb") as f:
            f.write(yaml.dump({}).encode())
        await self.git("add", ".mergify.yml")
        await self.git("commit", "--no-edit", "-m", "conf update")
        await self.git("push", "--quiet", "origin", self.main_branch_name)
        await self.run_engine()

        # we didn't change the pull request no configuration must be detected
        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["output"]["title"] == "no rules match, no planned actions"

    async def test_invalid_configuration_in_pull_request(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["base=main"],
                    "actions": {
                        "comment": {"message": "hello"},
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr(files={".mergify.yml": "not valid"})

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

    async def test_change_mergify_yml(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p1 = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            summary["output"]["title"]
            == "Configuration changed. This pull request must be merged manually — no rules match, no planned actions"
        )

    async def test_change_mergify_yml_in_meantime_on_big_pull_request(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p = await self.create_pr(files={f"f{i}": "data" for i in range(0, 160)})
        await self.run_engine()

        p_change_config = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.merge_pull(p_change_config["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert summary["output"]["title"] == "1 rule matches"

    async def test_invalid_action_option(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["base=main"],
                    "actions": {
                        "comment": {"unknown": "hello"},
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            summary["output"]["title"] == "The current Mergify configuration is invalid"
        )
        assert (
            summary["output"]["summary"]
            == "extra keys not allowed @ pull_request_rules → item 0 → actions → comment → unknown"
        )

    async def test_no_configuration(self) -> None:
        await self.setup_repo()
        p = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            "no rules configured, just listening for commands"
            == summary["output"]["title"]
        )

    async def test_configuration_deleted(self) -> None:
        await self.setup_repo("")
        await self.git("rm", "-rf", ".mergify.yml")
        p = await self.create_pr(git_tree_ready=True)
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            "Configuration changed. This pull request must be merged manually"
            in summary["output"]["title"]
        )
        additionnal_check = await ctxt.get_engine_check_run(
            "Configuration has been deleted"
        )
        assert additionnal_check is not None

    async def test_multiple_configurations(self) -> None:
        await self.setup_repo(
            files={
                ".mergify.yml": "",
                ".github/mergify.yml": "pull_request_rules: []",
            }
        )
        p = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            "Multiple Mergify configurations have been found in the repository"
            == summary["output"]["title"]
        )
        assert ".mergify.yml" in summary["output"]["summary"]
        assert ".github/mergify.yml" in summary["output"]["summary"]

    async def test_empty_configuration(self) -> None:
        await self.setup_repo("")
        p = await self.create_pr()
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert (
            "no rules configured, just listening for commands"
            == summary["output"]["title"]
        )

    async def test_merge_with_not_merged_attribute(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}", "-merged"],
                    "actions": {"merge": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        for check in await ctxt.pull_check_runs:
            if check["name"] == "Rule: merge on main (merge)":
                assert (
                    "The pull request has been merged automatically"
                    == check["output"]["title"]
                )
                assert (
                    f"The pull request has been merged automatically at *{ctxt.pull['merge_commit_sha']}*"
                    == check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Merge check not found")
