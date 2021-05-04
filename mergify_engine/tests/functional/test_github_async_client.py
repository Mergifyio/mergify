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

import yaml

from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.tests.functional import base


class TestGithubClient(base.FunctionalTestBase):
    async def test_github_async_client(self):

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
        await self.setup_repo(yaml.dump(rules), test_branches=[other_branch])
        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()
        await self.create_pr(base=other_branch)

        client = github.aget_client("mergifyio-testing")

        url = f"/repos/mergifyio-testing/{self.REPO_NAME}/pulls"

        pulls = [p async for p in client.items(url)]
        self.assertEqual(3, len(pulls))

        pulls = [p async for p in client.items(url, params={"per_page": 1})]
        self.assertEqual(3, len(pulls))

        pulls = [p async for p in client.items(url, params={"per_page": 1, "page": 2})]
        self.assertEqual(2, len(pulls))

        pulls = [
            p
            async for p in client.items(
                url, params={"base": other_branch, "state": "all"}
            )
        ]
        self.assertEqual(1, len(pulls))

        pulls = [p async for p in client.items(url, params={"base": "unknown"})]
        self.assertEqual(0, len(pulls))

        pull = await client.item(f"{url}/{p1['number']}")
        self.assertEqual(p1["number"], pull["number"])

        pull = await client.item(f"{url}/{p2['number']}")
        self.assertEqual(p2["number"], pull["number"])

        with self.assertRaises(http.HTTPStatusError) as ctxt:
            await client.item(f"{url}/10000000000")

        self.assertEqual(404, ctxt.exception.response.status_code)

    async def test_github_async_client_with_owner_id(self):

        rules = {
            "pull_request_rules": [
                {
                    "name": "fake PR",
                    "conditions": ["base=master"],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()
        url = f"/repos/mergifyio-testing/{self.REPO_NAME}/pulls"
        pulls = [p async for p in self.client_integration.items(url)]
        self.assertEqual(1, len(pulls))
