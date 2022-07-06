# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
import typing

import pytest
import yaml

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestMergeAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_merge_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=automerge",
                    ],
                    "actions": {"merge": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(draft=True)
        await self.add_label(p["number"], "automerge")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] is None
        assert check["output"]["title"] == "Draft flag needs to be removed"
        assert check["output"]["summary"] == ""

        await self.remove_label(p["number"], "automerge")
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "cancelled"
        assert check["output"]["title"] == "The rule doesn't match anymore"

    async def test_merge_with_installation_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        self.assertEqual(True, p["merged"])
        self.assertEqual(config.BOT_USER_LOGIN, p["merged_by"]["login"])

    async def test_merge_with_oauth_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {"merge_bot_account": "{{ body }}"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(message="mergify-test4")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        self.assertEqual(True, p["merged"])
        self.assertEqual("mergify-test4", p["merged_by"]["login"])

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "success"
        assert (
            check["output"]["title"] == "The pull request has been merged automatically"
        )
        assert (
            check["output"]["summary"]
            == f"The pull request has been merged automatically at *{p['merge_commit_sha']}*"
        )

    @pytest.mark.skipif(
        not config.GITHUB_URL.startswith("https://github.com"),
        reason="required_conversation_resolution requires GHES 3.2",
    )
    async def test_merge_branch_protection_conversation_resolution(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        protection = {
            "required_status_checks": None,
            "required_linear_history": False,
            "required_pull_request_reviews": None,
            "required_conversation_resolution": True,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p1 = await self.create_pr(
            files={"my_testing_file": "foo", "super_original_testfile": "42\ntest\n"}
        )

        await self.create_review_thread(
            p1["number"],
            "Don't like this line too much either",
            path="super_original_testfile",
            line=2,
        )

        thread = (await self.get_review_threads(p1["number"]))["repository"][
            "pullRequest"
        ]["reviewThreads"]["edges"][0]["node"]

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None

        assert (
            "- [ ] `#review-threads-unresolved=0` [🛡 GitHub branch protection]"
            in summary["output"]["summary"]
        )

        is_resolved = await self.resolve_review_thread(thread_id=thread["id"])
        assert is_resolved

        thread = (await self.get_review_threads(p1["number"]))["repository"][
            "pullRequest"
        ]["reviewThreads"]["edges"][0]["node"]
        assert thread["isResolved"]

        # NOTE(Syfe): We need to generate an event with send_pull_refresh() in order
        # to trigger the summary check update after resolve_review_thread() since GitHub doesn't
        # generate one after resolving a conversation (issue related MRGFY-907)
        await utils.send_pull_refresh(
            ctxt.redis.stream,
            ctxt.pull["base"]["repo"],
            pull_request_number=p1["number"],
            action="internal",
            source="test",
        )

        await self.run_engine()

        ctxt._caches.pull_check_runs.delete()
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None

        assert (
            "- [X] `#review-threads-unresolved=0` [🛡 GitHub branch protection]"
            in summary["output"]["summary"]
        )

    async def test_merge_branch_protection_linear_history(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        protection = {
            "required_status_checks": None,
            "required_linear_history": True,
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.main_branch_name, protection)

        p1 = await self.create_pr()
        await self.run_engine()
        await self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "failure" == checks[0]["conclusion"]
        assert (
            "Branch protection setting 'linear history' conflicts with Mergify configuration"
            == checks[0]["output"]["title"]
        )

    async def test_merge_template_with_empty_body(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "merge": {
                            "commit_message_template": """{{ title }} (#{{ number }})

{{body}}
""",
                        }
                    },
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(message="")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        self.assertEqual(True, p["merged"])
        c = await self.get_commit(p["merge_commit_sha"])
        assert (
            f"""test_merge_template_with_empty_body: pull request n1 from integration (#{p['number']})"""
            == c["commit"]["message"]
        )

    async def test_merge_template(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "merge": {
                            "commit_message_template": """{{ title }} (#{{ number }})
{{body}}
superRP!
""",
                        }
                    },
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(message="mergify-test4")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p2 = await self.get_pull(p["number"])
        self.assertEqual(True, p2["merged"])
        p3 = await self.get_commit(p2["merge_commit_sha"])
        assert (
            f"""test_merge_template: pull request n1 from integration (#{p2['number']})

mergify-test4
superRP!"""
            == p3["commit"]["message"]
        )
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert (
            """
:bangbang: **Action Required** :bangbang:

> **The configuration uses the deprecated `commit_message` mode of the merge action.**
> A brownout is planned for the whole March 21th, 2022 day.
> This option will be removed on April 25th, 2022.
> For more information: https://docs.mergify.com/actions/merge/

"""
            not in summary["output"]["summary"]
        )

    async def test_merge_branch_protection_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        p1 = await self.create_pr()
        p2 = await self.create_pr()

        await self.merge_pull(p1["number"])

        await self.branch_protection_protect(self.main_branch_name, protection)

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert "[ ] `#commits-behind=0`" in summary["output"]["summary"]

        await self.create_comment_as_admin(p2["number"], "@mergifyio update")
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        p2 = await self.get_pull(p2["number"])
        await self.create_status(p2)
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert "[X] `#commits-behind=0`" in summary["output"]["summary"]

        p2 = await self.get_pull(p2["number"])
        assert p2["merged"]

    async def test_merge_fastforward_basic(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "merge": {
                            "method": "fast-forward",
                        }
                    },
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(message="")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        assert p["merged"]
        assert p["merged_by"]["login"] == config.BOT_USER_LOGIN

        branch = typing.cast(
            github_types.GitHubBranch,
            await self.client_integration.item(
                f"{self.url_origin}/branches/{self.main_branch_name}"
            ),
        )
        assert p["head"]["sha"] == branch["commit"]["sha"]

        assert branch["commit"]["committer"] is not None
        assert branch["commit"]["committer"]["login"] == config.BOT_USER_LOGIN

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "success"
        assert check["output"]["title"] == "The pull request has been merged"
        assert (
            check["output"]["summary"]
            == f"The pull request has been merged at *{p['head']['sha']}*."
        )

    async def test_merge_fastforward_bot_account(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on main",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "merge": {
                            "method": "fast-forward",
                            "merge_bot_account": "{{ body }}",
                        }
                    },
                },
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p = await self.create_pr(message="mergify-test4")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        assert p["merged"]
        assert p["merged_by"]["login"] == "mergify-test4"

        branch = typing.cast(
            github_types.GitHubBranch,
            await self.client_integration.item(
                f"{self.url_origin}/branches/{self.main_branch_name}"
            ),
        )
        assert p["head"]["sha"] == branch["commit"]["sha"]

        assert branch["commit"]["committer"] is not None
        assert branch["commit"]["committer"]["login"] == config.BOT_USER_LOGIN
