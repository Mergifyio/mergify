# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mehdi Abaakouk <sileht@mergify.io>
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


import httpx
import yaml

from mergify_engine.clients import github
from mergify_engine.tests.functional import base


class TestGithubClient(base.FunctionalTestBase):
    def test_github_client(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "simulator",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        other_branch = self.get_full_branch_name("other")
        self.setup_repo(yaml.dump(rules), test_branches=[other_branch])
        p1, _ = self.create_pr()
        p2, _ = self.create_pr()
        self.create_pr(base=other_branch)

        installation = github.get_installation(
            self.o_integration.login, self.r_o_integration.name
        )
        client = github.get_client(
            self.o_integration.login, self.r_o_integration.name, installation
        )

        pulls = list(client.items("pulls"))
        self.assertEqual(2, len(pulls))

        pulls = list(client.items("pulls", per_page=1))
        self.assertEqual(2, len(pulls))

        pulls = list(client.items("pulls", per_page=1, page=2))
        self.assertEqual(1, len(pulls))

        pulls = list(client.items("pulls", base=other_branch, state="all"))
        self.assertEqual(1, len(pulls))

        pulls = list(client.items("pulls", base="unknown"))
        self.assertEqual(0, len(pulls))

        pull = client.item(f"pulls/{p1.number}")
        self.assertEqual(p1.number, pull["number"])

        pull = client.item(f"pulls/{p2.number}")
        self.assertEqual(p2.number, pull["number"])

        with self.assertRaises(httpx.HTTPError) as ctxt:
            client.item("pulls/10000000000")

        self.assertEqual(404, ctxt.exception.response.status_code)
