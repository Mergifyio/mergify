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
import datetime
import logging
from unittest import mock

from first import first
import pytest
import yaml

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine.queue import naive
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestMergeAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def _do_test_smart_order(self, strict):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on main",
                    "conditions": [f"base={self.main_branch_name}", "label=ready"],
                    "actions": {"merge": {"strict": strict}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p_need_rebase, _ = await self.create_pr(base_repo="origin")

        # To force previous to be rebased to be rebased
        p, _ = await self.create_pr(base_repo="origin")
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {})

        await self.git("fetch", "--all")
        p_ready, _ = await self.create_pr(base_repo="origin")

        await self.add_label(p_need_rebase["number"], "ready")
        await self.add_label(p_ready["number"], "ready")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()
        return p_need_rebase, p_ready

    async def test_merge_smart_ordered(self):
        p_need_rebase, p_ready = await self._do_test_smart_order("smart+ordered")
        ctxt = await context.Context.create(self.repository_ctxt, p_need_rebase, [])
        q = await naive.Queue.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p_ready["number"]]
        p_need_rebase = await self.get_pull(p_need_rebase["number"])
        assert p_need_rebase["merged"]
        assert p_need_rebase["commits"] == 2

    async def test_merge_smart_unordered(self):
        p_need_rebase, p_ready = await self._do_test_smart_order("smart+fastpath")
        ctxt = await context.Context.create(self.repository_ctxt, p_need_rebase, [])
        q = await naive.Queue.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == []
        p_ready = await self.get_pull(p_ready["number"])
        assert p_ready["merged"]
        p_need_rebase = await self.get_pull(p_need_rebase["number"])
        assert p_need_rebase["merged"]
        assert p_need_rebase["merged_at"] > p_ready["merged_at"]
        assert p_need_rebase["base"]["sha"] == p_ready["merge_commit_sha"]

    async def test_merge_smart_legacy(self):
        p_need_rebase, p_ready = await self._do_test_smart_order("smart")
        ctxt = await context.Context.create(self.repository_ctxt, p_need_rebase, [])
        q = await naive.Queue.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p_ready["number"]]
        p_need_rebase = await self.get_pull(p_need_rebase["number"])
        assert p_need_rebase["merged"]
        assert p_need_rebase["commits"] == 2

    async def test_merge_priority(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=high",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
                {
                    "name": "Merge priority default",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=medium",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=low",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered", "priority": 1}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p_high, _ = await self.create_pr()
        p_medium, _ = await self.create_pr()
        p_low, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine()

        # Merge them in reverse priority to ensure there are reordered
        await self.add_label(p_low["number"], "low")
        await self.create_status(p_low)
        await self.add_label(p_medium["number"], "medium")
        await self.create_status(p_medium)
        await self.add_label(p_high["number"], "high")
        await self.create_status(p_high)
        await self.run_engine(1)  # ensure we handle the 3 refresh here.

        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert "brownout" in summary["output"]["summary"]

        q = await naive.Queue.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p_high["number"], p_medium["number"], p_low["number"]]

        # Each PR can rebased, because we insert them in reserve order, but they are still
        # all in queue
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        p_high = await self.get_pull(p_high["number"])
        await self.create_status(p_high)
        # Ensure this events are proceed in same batch, otherwise replay may not work
        await self.run_engine()  # PR merged
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine(1)  # ensure we handle the 2 refresh here.
        await self.wait_for("pull_request", {"action": "synchronize"})

        p_medium = await self.get_pull(p_medium["number"])
        await self.create_status(p_medium)
        await self.run_engine()  # PR merged
        await self.wait_for("pull_request", {"action": "closed"})
        await self.run_engine(1)  # ensure we handle the last refresh here.
        await self.wait_for("pull_request", {"action": "synchronize"})

        p_low = await self.get_pull(p_low["number"])
        await self.create_status(p_low)
        await self.run_engine()  # PR merged
        await self.wait_for("pull_request", {"action": "closed"})

        p_low = await self.get_pull(p_low["number"])
        p_medium = await self.get_pull(p_medium["number"])
        p_high = await self.get_pull(p_high["number"])
        self.assertEqual(True, p_low["merged"])
        self.assertEqual(True, p_medium["merged"])
        self.assertEqual(True, p_high["merged"])

        assert (
            datetime.datetime.fromisoformat(p_low["merged_at"][:-1])
            > datetime.datetime.fromisoformat(p_medium["merged_at"][:-1])
            > datetime.datetime.fromisoformat(p_high["merged_at"][:-1])
        )

    async def test_merge_rule_switch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=high",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
                {
                    "name": "Merge priority medium",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=medium",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=low",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered", "priority": 1}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        # To force others to be rebased
        p, _ = await self.create_pr()
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        # Merge them in reverse priority to ensure there are reordered
        await self.add_label(p1["number"], "medium")
        await self.add_label(p2["number"], "low")
        await self.create_status(p1)
        await self.create_status(p2)
        await self.run_engine(1)

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        q = await naive.Queue.from_context(ctxt)
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p1["number"], p2["number"]]

        await self.remove_label(p2["number"], "low")
        await self.add_label(p2["number"], "high")
        await self.run_engine()
        pulls_in_queue = await q.get_pulls()
        assert pulls_in_queue == [p2["number"], p1["number"]]

    # FIXME(sileht): Provide a tools to generate oauth_token without
    # the need of the dashboard
    @pytest.mark.skipif(
        config.GITHUB_URL != "https://github.com",
        reason="We use a PAT token instead of an OAUTH_TOKEN",
    )
    async def test_merge_github_workflow(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=automerge",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(files={".github/workflows/foo.yml": "whatever"})
        await self.add_label(p["number"], "automerge")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "success"
        assert (
            check["output"]["title"] == "The pull request has been merged automatically"
        )

    async def test_merge_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=automerge",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(draft=True)
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

        p, _ = await self.create_pr()
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

        p, _ = await self.create_pr(message="mergify-test3")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        self.assertEqual(True, p["merged"])
        self.assertEqual("mergify-test3", p["merged_by"]["login"])

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

        p1, _ = await self.create_pr()
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

    async def test_merge_rule_deleted(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge me",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        # Force rebase
        p_rebase, _ = await self.create_pr()
        await self.merge_pull(p_rebase["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.create_status(p)
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        ctxt = context.Context(self.repository_ctxt, p)
        q = await naive.Queue.from_context(ctxt)
        assert len(await q.get_pulls()) == 1

        check = first(
            await context.Context(self.repository_ctxt, p).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge me (merge)",
        )
        assert check["conclusion"] is None
        assert (
            check["output"]["title"]
            == "The pull request is the 1st in the queue to be merged"
        )

        updated_rules = {
            "pull_request_rules": [
                {
                    "name": "Merge only if label is present",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "label=automerge",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
            ],
        }

        p2, _ = await self.create_pr(files={".mergify.yml": yaml.dump(updated_rules)})
        await self.merge_pull(p2["number"])
        await self.wait_for("push", {"ref": f"refs/heads/{self.main_branch_name}"})
        await self.run_engine()

        p = await self.get_pull(p["number"])
        check = first(
            await context.Context(self.repository_ctxt, p).pull_engine_check_runs,
            key=lambda c: c["name"] == "Rule: Merge me (merge)",
        )
        assert check["conclusion"] == "cancelled"
        assert check["output"]["title"] == "The rule/action does not exists anymore"
        q = await naive.Queue.from_context(ctxt)
        assert len(await q.get_pulls()) == 0

    @mock.patch.object(config, "ALLOW_MERGE_STRICT_MODE", False)
    async def test_strict_mode_brownout(self):

        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.main_branch_name}",
                        "label=high",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()
        await self.run_engine()

        checks = await context.Context(self.repository_ctxt, p).pull_engine_check_runs
        assert len(checks) == 1
        assert "failure" == checks[0]["conclusion"]
        assert "The Mergify configuration is invalid" == checks[0]["output"]["title"]
        assert (
            "extra keys not allowed @ pull_request_rules → item 0 → actions → merge → strict"
            == checks[0]["output"]["summary"]
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

        p, _ = await self.create_pr(message="mergify-test3")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p2 = await self.get_pull(p["number"])
        self.assertEqual(True, p2["merged"])
        p3 = await self.get_commit(p2["merge_commit_sha"])
        assert (
            f"""test_merge_template: pull request n1 from fork (#{p2['number']})

mergify-test3
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
