# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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


class TestQueueAction(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True

    def test_merge_priority(self):
        rules = {
            "queue_rules": [
                {
                    "name": "default",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                }
            ],
            "pull_request_rules": [
                {
                    "name": "Merge priority high",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=high",
                    ],
                    "actions": {"queue": {"name": "default", "priority": "high"}},
                },
                {
                    "name": "Merge priority default",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=medium",
                    ],
                    "actions": {"queue": {"name": "default"}},
                },
                {
                    "name": "Merge priority low",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=low",
                    ],
                    "actions": {"queue": {"name": "default", "priority": 1}},
                },
            ],
        }

        self.setup_repo(yaml.dump(rules))

        p_high, _ = self.create_pr()
        p_medium, _ = self.create_pr()
        p_low, _ = self.create_pr()

        # To force others to be rebased
        p, _ = self.create_pr()
        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),
        self.run_engine()

        # Merge them in reverse priority to ensure there are reordered
        self.add_label(p_low, "low")
        self.add_label(p_medium, "medium")
        self.add_label(p_high, "high")
        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_high.number, p_medium.number, p_low.number]

        # Add status for other PR, nothing should change
        self.create_status(p_medium)
        self.create_status(p_low)

        self.run_engine()

        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_high.number, p_medium.number, p_low.number]

        self.create_status(p_high)
        self.run_engine()
        self.wait_for("pull_request", {"action": "synchronize"})

        self.run_engine()
        p_high.update()
        self.create_status(p_high)
        self.run_engine()  # PR merged, refresh emitted on next PR
        self.wait_for("pull_request", {"action": "closed"})
        self.run_engine()  # exec the refresh

        self.wait_for("pull_request", {"action": "synchronize"})
        self.run_engine()
        p_medium.update()
        self.create_status(p_medium)
        self.run_engine()  # PR merged, refresh emitted on next PR
        self.wait_for("pull_request", {"action": "closed"})
        self.run_engine()  # exec the refresh

        self.wait_for("pull_request", {"action": "synchronize"})
        self.run_engine()
        p_low.update()
        self.create_status(p_low)
        self.run_engine()  # PR merged, refresh emitted on next PR
        self.wait_for("pull_request", {"action": "closed"})

        p_low = p_low.base.repo.get_pull(p_low.number)
        p_medium = p_medium.base.repo.get_pull(p_medium.number)
        p_high = p_high.base.repo.get_pull(p_high.number)
        self.assertEqual(True, p_low.merged)
        self.assertEqual(True, p_medium.merged)
        self.assertEqual(True, p_high.merged)

        assert p_low.merged_at > p_medium.merged_at > p_high.merged_at

    def test_merge_queue_ordering_and_cancellation(self):
        rules = {
            "queue_rules": [
                {
                    "name": "fast",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                },
                {
                    "name": "slow",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                        "status-success=continuous-integration/long-ci",
                    ],
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge fast",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=fast",
                    ],
                    "actions": {"queue": {"name": "fast"}},
                },
                {
                    "name": "Merge slow",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=slow",
                    ],
                    "actions": {"queue": {"name": "slow"}},
                },
            ],
        }

        self.setup_repo(yaml.dump(rules))

        p_slow, _ = self.create_pr()
        p_fast, _ = self.create_pr()

        self.add_label(p_slow, "slow")
        self.run_engine()

        ctxt = context.Context(self.cli_integration, p_slow.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_slow.number]

        self.add_label(p_fast, "fast")
        self.run_engine()
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_fast.number, p_slow.number]

        p_fast.remove_from_labels("fast")
        self.wait_for("pull_request", {"action": "unlabeled"})
        self.run_engine()
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_slow.number]

        self.add_label(p_fast, "slow")
        self.run_engine()
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_slow.number, p_fast.number]

        p_slow.remove_from_labels("slow")
        self.wait_for("pull_request", {"action": "unlabeled"})
        self.run_engine()
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_fast.number]

        self.add_label(p_slow, "slow")
        self.run_engine()
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_fast.number, p_slow.number]

    def test_merge_queue_with_check_failure(self):
        rules = {
            "queue_rules": [
                {
                    "name": "fast",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                    ],
                },
                {
                    "name": "slow",
                    "conditions": [
                        "status-success=continuous-integration/fake-ci",
                        "status-success=continuous-integration/long-ci",
                    ],
                },
            ],
            "pull_request_rules": [
                {
                    "name": "Merge fast",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=fast",
                    ],
                    "actions": {"queue": {"name": "fast"}},
                },
                {
                    "name": "Merge slow",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=slow",
                    ],
                    "actions": {"queue": {"name": "slow"}},
                },
            ],
        }

        self.setup_repo(yaml.dump(rules))

        p_slow, _ = self.create_pr()
        p_fast, _ = self.create_pr()

        self.add_label(p_slow, "slow")
        self.add_label(p_fast, "fast")
        self.run_engine()

        ctxt = context.Context(self.cli_integration, p_slow.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_fast.number, p_slow.number]

        self.create_status(p_fast, state="failure")
        self.run_engine()

        # check fail, pull is removed from queue and the user is informed with
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_slow.number]

        ctxt = context.Context(self.cli_integration, p_fast.raw_data, {})
        checks = list(
            c
            for c in ctxt.pull_engine_check_runs
            if c["name"] == "Rule: Merge fast (queue)"
        )
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "cancelled"
        assert (
            "The pull request have been removed from the queue"
            == checks[0]["output"]["title"]
        )
        assert (
            "The queue conditions cannot be reach due to failing checks."
            == checks[0]["output"]["summary"]
        )

        # user fixed the CI, queue is back in queue in correct order
        self.create_status(p_fast, state="pending")
        self.run_engine()

        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_fast.number, p_slow.number]
