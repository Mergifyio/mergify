# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
from mergify_engine.tests.functional import base


class TestCommandUpdate(base.FunctionalTestBase):
    async def test_command_update_noop(self) -> None:
        await self.setup_repo()
        p = await self.create_pr()
        await self.create_comment_as_admin(p["number"], "@mergifyio update")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2, comments
        assert "Nothing to do" in comments[-1]["body"]

    async def test_command_update_pending(self) -> None:
        await self.setup_repo()
        p = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.create_comment_as_admin(p["number"], "@mergifyio update")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2, comments
        assert "Nothing to do" in comments[-1]["body"]
