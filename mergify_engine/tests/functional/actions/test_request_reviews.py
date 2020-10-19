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
from unittest import mock

import pytest
import yaml

from mergify_engine import context
from mergify_engine.actions import request_reviews
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
        self.run_engine()

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
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)
        requests = pulls[0].get_review_requests()
        assert sorted([team.slug]) == sorted([team.slug for team in requests[1]])

    @mock.patch.object(
        request_reviews.RequestReviewsAction, "GITHUB_MAXIMUM_REVIEW_REQUEST", new=1
    )
    def test_request_reviews_already_max(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "approve",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"review": {"type": "APPROVE"}},
                },
                {
                    "name": "request_reviews",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {
                        "request_reviews": {"users": ["mergify-test1", "mergify-test"]}
                    },
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)
        requests = pulls[0].get_review_requests()
        assert ["mergify-test1"] == [user.login for user in requests[0]]

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        assert len(ctxt.pull_engine_check_runs) == 2
        for check in ctxt.pull_engine_check_runs:
            if check["name"] == "Rule: request_reviews (request_reviews)":
                assert "neutral" == check["conclusion"]
                assert (
                    "Maximum number of reviews already requested"
                    == check["output"]["title"]
                )
                assert (
                    "The maximum number of 1 reviews has been reached.\n"
                    "Unable to request reviews for additional users."
                    == check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Unable to find request review check run")

    @mock.patch.object(
        request_reviews.RequestReviewsAction, "GITHUB_MAXIMUM_REVIEW_REQUEST", new=2
    )
    def test_request_reviews_going_above_max(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "#review-requested>0",
                    ],
                    "actions": {
                        "request_reviews": {
                            "users": ["mergify-test1", "mergify-test3"],
                            "teams": ["mergifyio-testing/testing"],
                        }
                    },
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.run_engine()

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)
        pulls[0].create_review_request(["mergify-test1"])
        self.wait_for("pull_request", {"action": "review_requested"})
        self.run_engine()
        requests = pulls[0].get_review_requests()
        assert sorted(["mergify-test1", "mergify-test3"]) == sorted(
            [user.login for user in requests[0]]
        )

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        assert len(ctxt.pull_engine_check_runs) == 2
        for check in ctxt.pull_engine_check_runs:
            if check["name"] == "Rule: request_reviews (request_reviews)":
                assert "neutral" == check["conclusion"]
                assert (
                    "Maximum number of reviews already requested"
                    == check["output"]["title"]
                )
                assert (
                    "The maximum number of 2 reviews has been reached.\n"
                    "Unable to request reviews for additional users."
                    == check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Unable to find request review check run")
