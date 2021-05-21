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

import pytest
import yaml

from mergify_engine import config
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
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=ready"],
                    "actions": {"merge": {"strict": strict}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p_need_rebase, _ = await self.create_pr(base_repo="main")

        # To force previous to be rebased to be rebased
        p, _ = await self.create_pr(base_repo="main")
        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {})

        await self.git("fetch", "--all")
        p_ready, _ = await self.create_pr(base_repo="main")

        await self.add_label(p_need_rebase["number"], "ready")
        await self.add_label(p_ready["number"], "ready")
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
        assert pulls_in_queue == [p_need_rebase["number"]]
        p_ready = await self.get_pull(p_ready["number"])
        assert p_ready["merged"]

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
                        f"base={self.master_branch_name}",
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
                        f"base={self.master_branch_name}",
                        "label=medium",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.master_branch_name}",
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
                        f"base={self.master_branch_name}",
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
                        f"base={self.master_branch_name}",
                        "label=medium",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.master_branch_name}",
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
                        f"base={self.master_branch_name}",
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
        assert check["conclusion"] == "action_required"
        assert check["output"]["title"] == "Pull request must be merged manually."
        assert (
            check["output"]["summary"]
            == "GitHub App like Mergify are not allowed to merge pull request where `.github/workflows` is changed.\n<br />\nThis pull request must be merged manually."
        )

        await self.remove_label(p["number"], "automerge")
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "cancelled"
        assert check["output"]["title"] == "The rule doesn't match anymore"

    async def test_merge_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
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
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}"],
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
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {"merge_bot_account": "mergify-test3"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p = await self.get_pull(p["number"])
        self.assertEqual(True, p["merged"])
        self.assertEqual("mergify-test3", p["merged_by"]["login"])
