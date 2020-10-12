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

import yaml

from mergify_engine import context
from mergify_engine.actions.merge import queue
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestMergeAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    def _do_test_smart_order(self, strict):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=ready"],
                    "actions": {"merge": {"strict": strict}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p_need_rebase, _ = self.create_pr(base_repo="main")

        # To force previous to be rebased to be rebased
        p, _ = self.create_pr(base_repo="main")
        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),
        self.wait_for("push", {})

        self.git("fetch", "--all")
        p_ready, _ = self.create_pr(base_repo="main")

        self.add_label(p_need_rebase, "ready")
        self.add_label(p_ready, "ready")
        return p_need_rebase, p_ready

    def test_merge_smart_ordered(self):
        p_need_rebase, p_ready = self._do_test_smart_order("smart+ordered")
        ctxt = context.Context(self.cli_integration, p_need_rebase.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_need_rebase.number, p_ready.number]

    def test_merge_smart_unordered(self):
        p_need_rebase, p_ready = self._do_test_smart_order("smart+fastpath")
        ctxt = context.Context(self.cli_integration, p_need_rebase.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_need_rebase.number]
        p_ready.update()
        assert p_ready.merged

    def test_merge_smart_legacy(self):
        p_need_rebase, p_ready = self._do_test_smart_order("smart")
        ctxt = context.Context(self.cli_integration, p_need_rebase.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_need_rebase.number, p_ready.number]

    def test_merge_priority(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [f"base={self.master_branch_name}", "label=high"],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
                {
                    "name": "Merge priority default",
                    "conditions": [f"base={self.master_branch_name}", "label=medium"],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [f"base={self.master_branch_name}", "label=low"],
                    "actions": {"merge": {"strict": "smart+ordered", "priority": 1}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p_high, _ = self.create_pr()
        p_medium, _ = self.create_pr()
        p_low, _ = self.create_pr()

        # To force others to be rebased
        p, _ = self.create_pr()
        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        # Merge them in reverse priority to ensure there are reordered
        self.add_label(p_low, "low")
        self.add_label(p_medium, "medium")
        self.add_label(p_high, "high")

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_high.number, p_medium.number, p_low.number]

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        p_low.update()
        p_medium.update()
        p_high.update()
        self.assertEqual(True, p_low.merged)
        self.assertEqual(True, p_high.merged)
        self.assertEqual(True, p_medium.merged)
        assert p_low.merged_at > p_medium.merged_at > p_high.merged_at

    def test_merge_rule_switch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [f"base={self.master_branch_name}", "label=high"],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
                {
                    "name": "Merge priority medium",
                    "conditions": [f"base={self.master_branch_name}", "label=medium"],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [f"base={self.master_branch_name}", "label=low"],
                    "actions": {"merge": {"strict": "smart+ordered", "priority": 1}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr()
        p2, _ = self.create_pr()

        # To force others to be rebased
        p, _ = self.create_pr()
        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        # Merge them in reverse priority to ensure there are reordered
        self.add_label(p1, "medium")
        self.add_label(p2, "low")

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p1.number, p2.number]

        # NOTE(sileht): The removal and the add must be part of the same batch to make the
        # test useful
        p2.remove_from_labels("low")
        self.add_label(p2, "high")
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p2.number, p1.number]

    def test_merge_github_workflow(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr(files={".github/workflows/foo.yml": "whatever"})

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = ctxt.pull_engine_check_runs
        assert len(checks) == 2
        check = checks[1]
        assert check["conclusion"] == "action_required"
        assert check["output"]["title"] == "Pull request must be merged manually."
        assert (
            check["output"]["summary"]
            == "GitHub App like Mergify are not allowed to merge pull request where `.github/workflows` is changed."
        )

    def test_merge_with_installation_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.wait_for("pull_request", {"action": "closed"})

        p.update()
        self.assertEqual(True, p.merged)
        self.assertEqual("mergify-test[bot]", p.merged_by.login)

    def test_merge_with_oauth_token(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {"merge_bot_account": "mergify-test1"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.wait_for("pull_request", {"action": "closed"})

        p.update()
        self.assertEqual(True, p.merged)
        self.assertEqual("mergify-test1", p.merged_by.login)


class TestMergeNoSubAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = False

    def test_merge_priority(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [f"base={self.master_branch_name}", "label=high"],
                    "actions": {
                        "merge": {"strict": "smart+ordered", "priority": "high"}
                    },
                },
                {
                    "name": "Merge priority default",
                    "conditions": [f"base={self.master_branch_name}", "label=medium"],
                    "actions": {"merge": {"strict": "smart+ordered"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [f"base={self.master_branch_name}", "label=low"],
                    "actions": {"merge": {"strict": "smart+ordered", "priority": 1}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p_high, _ = self.create_pr()
        p_medium, _ = self.create_pr()
        p_low, _ = self.create_pr()

        # To force others to be rebased
        p, _ = self.create_pr()
        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        # Merge them in reverse priority to ensure there are reordered
        self.add_label(p_low, "low")
        self.add_label(p_medium, "medium")
        self.add_label(p_high, "high")

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_low.number, p_medium.number, p_high.number]

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        queue.Queue.process_queues()
        self.wait_for("pull_request", {"action": "closed"})

        p_low.update()
        p_medium.update()
        p_high.update()
        self.assertEqual(True, p_low.merged)
        self.assertEqual(True, p_high.merged)
        self.assertEqual(True, p_medium.merged)
        assert p_high.merged_at > p_medium.merged_at > p_low.merged_at
