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
        p_need_rebase, p_ready = self._do_test_smart_order("smart")
        ctxt = context.Context(self.cli_integration, p_need_rebase.raw_data, {})
        q = queue.Queue.from_context(ctxt)
        pulls_in_queue = q.get_pulls()
        assert pulls_in_queue == [p_need_rebase.number]
        p_ready.update()
        assert p_ready.merged
