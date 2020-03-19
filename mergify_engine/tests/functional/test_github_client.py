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
                    "conditions": ["base!=master"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), test_branches=["other"])
        self.create_pr()
        self.create_pr()
        self.create_pr(base="other")

        client = github.get_client(self.o_integration.login, self.r_o_integration.name)

        pulls = list(client.items("pulls"))
        self.assertEqual(2, len(pulls))

        pulls = list(client.items("pulls", per_page=1))
        self.assertEqual(2, len(pulls))

        pulls = list(client.items("pulls", per_page=1, page=2))
        self.assertEqual(1, len(pulls))

        pulls = list(client.items("pulls", base="other", state="all"))
        self.assertEqual(1, len(pulls))

        pulls = list(client.items("pulls", base="unknown"))
        self.assertEqual(0, len(pulls))

        pull = client.item("pulls/1")
        self.assertEqual(1, pull["number"])

        pull = client.item("pulls/2")
        self.assertEqual(2, pull["number"])

        with self.assertRaises(httpx.HTTPError) as ctxt:
            client.item("pulls/4")

        self.assertEqual(404, ctxt.exception.response.status_code)
