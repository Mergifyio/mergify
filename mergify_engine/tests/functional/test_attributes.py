# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mehdi Abaakouk <sileht@mergify.io>
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
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestAttributes(base.FunctionalTestBase):
    def test_draft(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["draft"],
                    "actions": {"comment": {"message": "draft pr"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))

        pr, _ = self.create_pr()
        pull = context.Context(self.cli_integration, {"number": pr.number})
        assert not pull.get_consolidated_data("draft")

        pr, _ = self.create_pr(draft=True)

        self.wait_for("issue_comment", {"action": "created"})

        pull = context.Context(self.cli_integration, {"number": pr.number})
        assert pull.get_consolidated_data("draft")

        pr.update()
        comments = list(pr.get_issue_comments())
        self.assertEqual("draft pr", comments[-1].body)
