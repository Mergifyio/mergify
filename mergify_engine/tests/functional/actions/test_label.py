# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Julien Danjou <jd@mergify.io>
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

from mergify_engine.tests.functional import base


class TestLabelAction(base.FunctionalTestBase):
    async def test_label_basic(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "label": {
                            "add": ["unstable", "foobar", "vector"],
                            "remove": ["stable", "what", "remove-me"],
                        }
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()

        # NOTE(sileht): We create first a label with a wrong case, GitHub
        # label... you can't have a label twice with different case.
        await self.add_label(p["number"], "vEcToR")
        await self.add_label(p["number"], "ReMoVe-Me")
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["unstable", "foobar", "vEcToR"]),
            sorted(label["name"] for label in pulls[0]["labels"]),
        )

        # Ensure it's idempotant
        await self.remove_label(p["number"], "unstable")
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["unstable", "foobar", "vEcToR"]),
            sorted(label["name"] for label in pulls[0]["labels"]),
        )

    async def test_label_empty(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": [f"base={self.main_branch_name}", "label=stable"],
                    "actions": {
                        "label": {
                            "add": [],
                            "remove": [],
                        }
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.add_label(p["number"], "stable")
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["stable"]),
            sorted(label["name"] for label in pulls[0]["labels"]),
        )

    async def test_label_remove_all(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete all labels",
                    "conditions": [f"base={self.main_branch_name}", "label=stable"],
                    "actions": {"label": {"remove_all": True}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.add_label(p["number"], "stable")
        await self.run_engine()

        pulls = await self.get_pulls()
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            [],
            pulls[0]["labels"],
        )
