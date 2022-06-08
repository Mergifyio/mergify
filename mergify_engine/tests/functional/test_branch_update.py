# -*- encoding: utf-8 -*-
#
# Copyright © 2020  Mergify SAS
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


import base64
import typing

import yaml

from mergify_engine import github_types
from mergify_engine.tests.functional import base


class TestBranchUpdatePublic(base.FunctionalTestBase):
    async def test_command_update(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "nothing"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1 = await self.create_pr(files={"TESTING2": "foobar"})
        p2 = await self.create_pr(files={"TESTING3": "foobar"})
        await self.merge_pull(p1["number"])

        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_comment_as_admin(p2["number"], "@mergifyio update")
        await self.run_engine()

        oldsha = p2["head"]["sha"]
        p2 = await self.get_pull(p2["number"])
        assert p2["commits"] == 2
        assert oldsha != p2["head"]["sha"]

    async def test_command_rebase_ok(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-label",
                    "conditions": ["label=rebase"],
                    "actions": {"comment": {"message": "nothing"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar\n"})
        p1 = await self.create_pr(files={"TESTING": "foobar\n\n\np1"})
        p2 = await self.create_pr(files={"TESTING": "p2\n\nfoobar\n"})
        await self.merge_pull(p1["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_comment_as_admin(p2["number"], "@mergifyio rebase")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        oldsha = p2["head"]["sha"]
        await self.merge_pull(p2["number"])
        p2 = await self.get_pull(p2["number"])
        assert oldsha != p2["head"]["sha"]
        f = typing.cast(
            github_types.GitHubContentFile,
            await self.client_integration.item(f"{self.url_origin}/contents/TESTING"),
        )
        data = base64.b64decode(bytearray(f["content"], "utf-8"))
        assert data == b"p2\n\nfoobar\n\n\np1"


# FIXME(sileht): This is not yet possible, due to GH restriction ...
# class TestBranchUpdatePrivate(TestBranchUpdatePublic):
#    REPO_NAME = "functional-testing-repo-private"
#    FORK_PERSONAL_TOKEN = config.ORG_USER_PERSONAL_TOKEN
#    SUBSCRIPTION_ACTIVE = True
