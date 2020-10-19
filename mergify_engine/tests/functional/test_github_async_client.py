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

import asyncio

import yaml

from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.tests.functional import base


class TestGithubClient(base.FunctionalTestBase):
    def test_github_async_client(self):

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

        async def _test_github_async_client():
            client = await github.aget_client(self.o_integration.login)

            url = f"/repos/{self.o_integration.login}/{self.r_o_integration.name}/pulls"

            pulls = [p async for p in client.items(url)]
            self.assertEqual(3, len(pulls))

            pulls = [p async for p in client.items(url, per_page=1)]
            self.assertEqual(3, len(pulls))

            pulls = [p async for p in client.items(url, per_page=1, page=2)]
            self.assertEqual(2, len(pulls))

            pulls = [p async for p in client.items(url, base=other_branch, state="all")]
            self.assertEqual(1, len(pulls))

            pulls = [p async for p in client.items(url, base="unknown")]
            self.assertEqual(0, len(pulls))

            pull = await client.item(f"{url}/{p1.number}")
            self.assertEqual(p1.number, pull["number"])

            pull = await client.item(f"{url}/{p2.number}")
            self.assertEqual(p2.number, pull["number"])

            with self.assertRaises(http.HTTPStatusError) as ctxt:
                await client.item(f"{url}/10000000000")

            self.assertEqual(404, ctxt.exception.response.status_code)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(_test_github_async_client())
