# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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
import logging

import pytest
import yaml

from mergify_engine import context
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestAttributes(base.FunctionalTestBase):
    async def test_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["draft"],
                    "actions": {"comment": {"message": "draft pr"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }
        await self.branch_protection_protect(self.master_branch_name, protection)

        pr, _ = await self.create_pr()
        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert not await ctxt.pull_request.draft

        pr, _ = await self.create_pr(draft=True)

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        ctxt = await context.Context.create(
            self.repository_ctxt,
            {
                "number": pr["number"],
                "base": {
                    "user": {"login": pr["base"]["user"]["login"]},
                    "repo": {
                        "name": pr["base"]["repo"]["name"],
                    },
                },
            },
        )
        assert await ctxt.pull_request.draft

        comments = await self.get_issue_comments(pr["number"])
        self.assertEqual("draft pr", comments[-1]["body"])

        # Test underscore/dash attributes
        assert await ctxt.pull_request.review_requested == []

        with pytest.raises(AttributeError):
            assert await ctxt.pull_request.foobar

        # Test items
        assert list(ctxt.pull_request) == list(
            context.PullRequest.ATTRIBUTES | context.PullRequest.LIST_ATTRIBUTES
        )
        assert await ctxt.pull_request.items() == {
            "number": pr["number"],
            "closed": False,
            "locked": False,
            "assignee": [],
            "approved-reviews-by": [],
            "files": ["test2"],
            "check-neutral": [],
            "status-neutral": [],
            "commented-reviews-by": [],
            "milestone": "",
            "label": [],
            "body": "test_draft: pull request n2 from fork",
            "base": self.master_branch_name,
            "review-requested": [],
            "check-success": ["Summary"],
            "status-success": ["Summary"],
            "changes-requested-reviews-by": [],
            "merged": False,
            "head": self.get_full_branch_name("fork/pr2"),
            "author": "mergify-test2",
            "dismissed-reviews-by": [],
            "merged-by": "",
            "check-failure": [],
            "status-failure": [],
            "title": "test_draft: pull request n2 from fork",
            "conflict": False,
            "check-pending": ["continuous-integration/fake-ci"],
            "check-stale": [],
            "check-success-or-neutral": ["Summary"],
            "check-skipped": [],
        }


class TestAttributesWithSub(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_depends_on(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=automerge",
                    ],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        pr1, _ = await self.create_pr()
        pr2, _ = await self.create_pr()
        await self.merge_pull(pr2["number"])

        body = f"Awesome body\nDepends-On: #{pr1['number']}\ndepends-on: #{pr2['number']}\ndepends-On: #9999999"
        pr, _ = await self.create_pr(message=body)
        await self.add_label(pr["number"], "automerge")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, pr)
        assert ctxt.get_depends_on() == {pr1["number"], pr2["number"], 9999999}
        assert await ctxt._get_consolidated_data("depends-on") == [f"#{pr2['number']}"]

        repo_url = ctxt.pull["base"]["repo"]["html_url"]
        summary = [
            c for c in await ctxt.pull_engine_check_runs if c["name"] == "Summary"
        ][0]
        expected = f"""#### Rule: merge (merge)
- [X] `base={self.master_branch_name}`
- [X] `label=automerge`
- [ ] `depends-on=#{pr1['number']}` [⛓️ **test_depends_on: pull request n1 from fork** ([#{pr1['number']}]({repo_url}/pull/{pr1['number']}))]
- [X] `depends-on=#{pr2['number']}` [⛓️ **test_depends_on: pull request n2 from fork** ([#{pr2['number']}]({repo_url}/pull/{pr2['number']}))]
- [ ] `depends-on=#9999999` [⛓️ ⚠️ *pull request not found* (#9999999)]
"""
        assert expected == summary["output"]["summary"][: len(expected)]
