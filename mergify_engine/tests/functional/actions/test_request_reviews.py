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
    async def test_request_reviews_users(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"request_reviews": {"users": ["mergify-test1"]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        assert 1 == len(pulls)
        requests = await self.get_review_requests(pulls[0]["number"])
        assert sorted(["mergify-test1"]) == sorted(
            user["login"] for user in requests["users"]
        )

    async def test_request_reviews_teams(self):
        team = (await self.get_teams())[0]
        await self.add_team_permission(team["slug"], "push")

        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"request_reviews": {"teams": [team["slug"]]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        assert 1 == len(pulls)
        requests = await self.get_review_requests(pulls[0]["number"])
        assert sorted([team["slug"]]) == sorted(
            team["slug"] for team in requests["teams"]
        )

    @mock.patch.object(
        request_reviews.RequestReviewsAction, "GITHUB_MAXIMUM_REVIEW_REQUEST", new=1
    )
    async def test_request_reviews_already_max(self):
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

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        assert 1 == len(pulls)
        requests = await self.get_review_requests(pulls[0]["number"])
        assert ["mergify-test1"] == [user["login"] for user in requests["users"]]

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        for check in checks:
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
    async def test_request_reviews_going_above_max(self):
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

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()

        pulls = await self.get_pulls(params={"base": self.master_branch_name})
        assert 1 == len(pulls)
        await self.create_review_request(pulls[0]["number"], ["mergify-test1"])
        await self.run_engine()
        requests = await self.get_review_requests(pulls[0]["number"])
        assert sorted(["mergify-test1", "mergify-test3"]) == sorted(
            user["login"] for user in requests["users"]
        )

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 2
        for check in checks:
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
