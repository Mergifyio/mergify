# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
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


class TestCommandBackport(base.FunctionalTestBase):
    async def test_command_backport(self) -> None:
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")
        await self.setup_repo(test_branches=[stable_branch, feature_branch])
        p = await self.create_pr()

        await self.run_engine()
        await self.create_comment_as_admin(
            p["number"], f"@mergifyio backport {stable_branch} {feature_branch}"
        )
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2, comments
        assert "Waiting for conditions" in comments[-1]["body"]
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        pulls_stable = await self.get_pulls(
            params={"state": "all", "base": stable_branch}
        )
        assert 1 == len(pulls_stable)
        pulls_feature = await self.get_pulls(
            params={"state": "all", "base": feature_branch}
        )
        assert 1 == len(pulls_feature)
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 3
        assert "Backports have been created" in comments[-1]["body"]
        reactions = [
            r
            async for r in self.client_integration.items(
                f"{self.url_origin}/issues/comments/{comments[0]['id']}/reactions",
                api_version="squirrel-girl",
                resource_name="reactions",
                page_limit=5,
            )
        ]
        assert len(reactions) == 1
        assert "+1" == reactions[0]["content"]

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_origin, ["mergify/bp"])
        ]
        assert sorted(refs) == [
            f"refs/heads/mergify/bp/{feature_branch}/pr-{p['number']}",
            f"refs/heads/mergify/bp/{stable_branch}/pr-{p['number']}",
        ]

        await self.merge_pull(pulls_feature[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.merge_pull(pulls_stable[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_origin, ["mergify/bp"])
        ]
        assert refs == []

    async def test_command_backport_with_defaults(self) -> None:
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")
        rules = {
            "defaults": {
                "actions": {"backport": {"branches": [stable_branch, feature_branch]}},
            },
        }

        await self.setup_repo(
            yaml.dump(rules), test_branches=[stable_branch, feature_branch]
        )
        p = await self.create_pr()
        await self.create_comment_as_admin(
            p["number"], f"@mergifyio backport {stable_branch} {feature_branch}"
        )
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        pulls = await self.get_pulls(params={"state": "all", "base": stable_branch})
        assert 1 == len(pulls)
        pulls = await self.get_pulls(params={"state": "all", "base": feature_branch})
        assert 1 == len(pulls)
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2
        reactions = [
            r
            async for r in self.client_integration.items(
                f"{self.url_origin}/issues/comments/{comments[0]['id']}/reactions",
                api_version="squirrel-girl",
                resource_name="reactions",
                page_limit=5,
            )
        ]
        assert len(reactions) == 1
        assert "+1" == reactions[0]["content"]

    async def test_command_backport_without_config(self) -> None:
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")
        await self.setup_repo(test_branches=[stable_branch, feature_branch])
        p = await self.create_pr()

        await self.create_comment_as_admin(
            p["number"], f"@mergifyio backport {stable_branch} {feature_branch}"
        )
        await self.run_engine()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        pulls_stable = await self.get_pulls(
            params={"state": "all", "base": stable_branch}
        )
        assert 1 == len(pulls_stable)
        pulls_feature = await self.get_pulls(
            params={"state": "all", "base": feature_branch}
        )
        assert 1 == len(pulls_feature)
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 3
        reactions = [
            r
            async for r in self.client_integration.items(
                f"{self.url_origin}/issues/comments/{comments[0]['id']}/reactions",
                api_version="squirrel-girl",
                resource_name="reactions",
                page_limit=5,
            )
        ]
        assert len(reactions) == 1
        assert "+1" == reactions[0]["content"]

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_origin, ["mergify/bp"])
        ]
        assert sorted(refs) == [
            f"refs/heads/mergify/bp/{feature_branch}/pr-{p['number']}",
            f"refs/heads/mergify/bp/{stable_branch}/pr-{p['number']}",
        ]

        await self.merge_pull(pulls_feature[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.merge_pull(pulls_stable[0]["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_origin, ["mergify/bp"])
        ]
        assert refs == []

        # Ensure summary have been posted
        ctxt = await self.repository_ctxt.get_pull_request_context(p["number"], p)
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1

    async def test_command_edited_by_human(self) -> None:
        feature_branch = self.get_full_branch_name("feature/one")
        await self.setup_repo(test_branches=[feature_branch])
        p = await self.create_pr()

        await self.run_engine()
        await self.create_comment_as_admin(
            p["number"], f"@mergifyio backport {feature_branch}"
        )
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 2, comments
        assert "Waiting for conditions" in comments[-1]["body"]
        await self.client_admin.post(
            f"{self.repository_ctxt.base_url}/issues/comments/{comments[-1]['id']}",
            json={"body": f"{comments[-1]['body']}\neheh!!"},
        )
        await self.wait_for("issue_comment", {"action": "edited"})
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 3, comments
        assert "eheh" in comments[-2]["body"]
        assert "Command aborted" in comments[-1]["body"]

        # rerun ensure it's idempotent
        await self.run_engine()
        comments = await self.get_issue_comments(p["number"])
        assert len(comments) == 3, comments
        assert "eheh" in comments[-2]["body"]
        assert "Command aborted" in comments[-1]["body"]
