# -*- encoding: utf-8 -*-
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


class TestCommandCopy(base.FunctionalTestBase):
    async def test_command_copy(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-copy",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {
                        "comment": {
                            "message": f"@mergifyio copy {stable_branch} {feature_branch}"
                        }
                    },
                }
            ]
        }

        await self.setup_repo(
            yaml.dump(rules), test_branches=[stable_branch, feature_branch]
        )
        p, _ = await self.create_pr()

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()

        pulls_stable = await self.get_pulls(
            params={"state": "all", "base": stable_branch}
        )
        assert 1 == len(pulls_stable)
        pulls_feature = await self.get_pulls(
            params={"state": "all", "base": feature_branch}
        )
        assert 1 == len(pulls_feature)
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2
        reactions = [
            r
            async for r in self.client_admin.items(
                f"{self.url_main}/issues/comments/{comments[0]['id']}/reactions",
                api_version="squirrel-girl",
            )
        ]
        assert len(reactions) == 1
        assert "+1" == reactions[0]["content"]

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_main, ["mergify/copy"])
        ]
        assert sorted(refs) == [
            f"refs/heads/mergify/copy/{feature_branch}/pr-{p['number']}",
            f"refs/heads/mergify/copy/{stable_branch}/pr-{p['number']}",
        ]

        await self.merge_pull(pulls_feature[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.merge_pull(pulls_stable[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_main, ["mergify/copy"])
        ]
        assert refs == []
