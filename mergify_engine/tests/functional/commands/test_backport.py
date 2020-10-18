# -*- encoding: utf-8 -*-
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
    def test_command_backport(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        feature_branch = self.get_full_branch_name("feature/one")
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-backport",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {
                        "comment": {
                            "message": f"@mergifyio backport {stable_branch} {feature_branch}"
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch, feature_branch])
        p, _ = self.create_pr()

        self.run_engine()
        self.wait_for("issue_comment", {"action": "created"})

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})
        self.run_engine()
        self.wait_for("issue_comment", {"action": "created"})

        pulls = list(self.r_o_admin.get_pulls(state="all", base=stable_branch))
        assert 1 == len(pulls)
        pulls = list(self.r_o_admin.get_pulls(state="all", base=feature_branch))
        assert 1 == len(pulls)
        assert ["+1"] == [
            r.content for c in p.get_issue_comments() for r in c.get_reactions()
        ]
