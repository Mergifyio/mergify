# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2022 Mergify SAS
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
import logging

import yaml

from mergify_engine import constants
from mergify_engine import context
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestSummary(base.FunctionalTestBase):
    """Mergify engine summary tests.

    Github resources are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    async def test_failed_base_changeable_attributes_rules_in_not_applicable_summary_section_with_basic_conditions(
        self,
    ) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "invalid rule dummy",
                    "conditions": [
                        "base=dummy",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "valid rule main",
                    "conditions": [
                        f"base={self.main_branch_name}",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert summary["output"]["title"] == "1 rule matches"
        assert "1 not applicable rule" in summary["output"]["summary"]

    async def test_failed_base_changeable_attributes_rules_in_not_applicable_summary_section_with_or_condition(
        self,
    ) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "invalid rule dummy",
                    "conditions": [
                        "base=dummy",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "valid rule label",
                    "conditions": [
                        {
                            "or": [
                                "label=test",
                                "base=dummy",
                            ]
                        },
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.add_label(p["number"], "test")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert summary["output"]["title"] == "1 rule matches"
        assert "1 not applicable rule" in summary["output"]["summary"]
