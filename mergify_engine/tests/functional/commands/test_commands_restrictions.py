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


class TestCommandsRestrictions(base.FunctionalTestBase):
    async def test_commands_restrictions(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")

        await self.setup_repo(
            yaml.dump(
                {
                    "commands_restrictions": {
                        "copy": {"conditions": [f"base={self.main_branch_name}"]}
                    }
                }
            ),
            test_branches=[stable_branch, feature_branch],
        )
        p_ok = await self.create_pr()
        p_nok = await self.create_pr(base=feature_branch)

        await self.create_comment_as_admin(
            p_ok["number"], f"@mergifyio copy {stable_branch}"
        )
        await self.create_comment_as_admin(
            p_nok["number"], f"@mergifyio copy {stable_branch}"
        )
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        await self.wait_for("issue_comment", {"action": "created"})
        await self.run_engine()

        # only one copy done
        pulls_stable = await self.get_pulls(
            params={"state": "all", "base": stable_branch}
        )
        assert 1 == len(pulls_stable)
        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_origin, ["mergify/copy"])
        ]
        assert sorted(refs) == [
            f"refs/heads/mergify/copy/{stable_branch}/pr-{p_ok['number']}",
        ]

        # success comment
        comments = await self.get_issue_comments(p_ok["number"])
        assert len(comments) == 2
        assert "Pull request copies have been created" in comments[1]["body"]

        # failure comment
        comments = await self.get_issue_comments(p_nok["number"])
        assert len(comments) == 2
        assert "Command disallowed on this pull request" in comments[1]["body"]
