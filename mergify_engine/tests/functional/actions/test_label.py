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
    def test_label(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": [f"base={self.master_branch_name}", "label=stable"],
                    "actions": {
                        "label": {
                            "add": ["unstable", "foobar"],
                            "remove": ["stable", "what"],
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.add_label(p, "stable")
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["unstable", "foobar"]),
            sorted([label.name for label in pulls[0].labels]),
        )

    def test_label_empty(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": [f"base={self.master_branch_name}", "label=stable"],
                    "actions": {
                        "label": {
                            "add": [],
                            "remove": [],
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.add_label(p, "stable")
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["stable"]), sorted([label.name for label in pulls[0].labels])
        )

    def test_label_remove_all(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete all labels",
                    "conditions": [f"base={self.master_branch_name}", "label=stable"],
                    "actions": {"label": {"remove_all": True}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.add_label(p, "stable")
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            [],
            pulls[0].labels,
        )
