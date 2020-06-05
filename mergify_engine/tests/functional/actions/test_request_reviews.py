# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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


class TestRequestReviewsAction(base.FunctionalTestBase):
    def test_request_reviews_users(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"request_reviews": {"users": ["mergify-test1"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)
        requests = pulls[0].get_review_requests()
        assert sorted(["mergify-test1"]) == sorted([user.login for user in requests[0]])

    def test_request_reviews_teams(self):
        # Add a team to the repo with write permissions  so it can review
        team = list(self.o_admin.get_teams())[0]
        team.set_repo_permission(self.r_o_admin, "push")

        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"request_reviews": {"teams": [team.slug]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)
        requests = pulls[0].get_review_requests()
        assert sorted([team.slug]) == sorted([team.slug for team in requests[1]])
