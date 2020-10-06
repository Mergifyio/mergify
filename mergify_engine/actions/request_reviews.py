# Copyright © 2019–2020 Mergify SAS
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
import itertools
import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import http
from mergify_engine.rules import types


class RequestReviewsAction(actions.Action):

    # This is the maximum number of review you can request on a PR.
    # It's not documented in the API, but it is shown in GitHub UI.
    # Any review passed that number is ignored by GitHub API.
    GITHUB_MAXIMUM_REVIEW_REQUEST = 15

    _random_weight = voluptuous.Required(
        voluptuous.All(int, voluptuous.Range(min=1, max=65535)), default=1
    )

    validator = {
        voluptuous.Required("users", default=[]): voluptuous.Any(
            [types.GitHubLogin],
            {
                types.GitHubLogin: _random_weight,
            },
        ),
        voluptuous.Required("teams", default=[]): voluptuous.Any(
            [types.GitHubTeam],
            {
                types.GitHubTeam: _random_weight,
            },
        ),
        "random_count": voluptuous.All(
            int,
            voluptuous.Range(1, GITHUB_MAXIMUM_REVIEW_REQUEST),
        ),
    }

    silent_report = True

    always_run = True

    def _get_random_reviewers(self, random_number: int, pr_author: str) -> set:
        if isinstance(self.config["users"], dict):
            user_weights = self.config["users"]
        else:
            user_weights = {user: 1 for user in self.config["users"]}

        if isinstance(self.config["teams"], dict):
            team_weights = self.config["teams"]
        else:
            team_weights = {team: 1 for team in self.config["teams"]}

        choices = {
            **{user.lower(): weight for user, weight in user_weights.items()},
            **{f"@{team}": weight for team, weight in team_weights.items()},
        }

        try:
            del choices[pr_author.lower()]
        except KeyError:
            pass

        count = min(self.config["random_count"], len(choices))

        return utils.get_random_choices(
            random_number,
            choices,
            count,
        )

    def _get_reviewers(
        self, pr_id: int, existing_reviews: set, pr_author: str
    ) -> typing.Tuple[typing.Set[str], typing.Set[str]]:
        if "random_count" in self.config:
            team_reviews_to_request = set()
            user_reviews_to_request = set()

            for reviewer in self._get_random_reviewers(pr_id, pr_author):
                if reviewer.startswith("@"):
                    team_reviews_to_request.add(reviewer[1:])
                else:
                    user_reviews_to_request.add(reviewer)
        else:
            user_reviews_to_request = set(self.config["users"])
            team_reviews_to_request = set(self.config["teams"])

        user_reviews_to_request -= existing_reviews
        user_reviews_to_request -= {pr_author}

        # Team starts with @
        team_reviews_to_request -= {
            e[1:] for e in existing_reviews if e.startswith("@")
        }

        return user_reviews_to_request, team_reviews_to_request

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        if "random_count" in self.config and not ctxt.subscription.has_feature(
            subscription.Features.RANDOM_REQUEST_REVIEWS
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Random request reviews are disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        # Using consolidated data to avoid already done API lookup
        reviews_keys = (
            "approved-reviews-by",
            "dismissed-reviews-by",
            "changes-requested-reviews-by",
            "commented-reviews-by",
            "review-requested",
        )
        existing_reviews = set(
            itertools.chain(*[getattr(ctxt.pull_request, key) for key in reviews_keys])
        )

        user_reviews_to_request, team_reviews_to_request = self._get_reviewers(
            ctxt.pull["id"],
            existing_reviews,
            ctxt.pull["user"]["login"],
        )

        if user_reviews_to_request or team_reviews_to_request:
            requested_reviews_nb = len(ctxt.pull_request.review_requested)

            already_at_max = requested_reviews_nb == self.GITHUB_MAXIMUM_REVIEW_REQUEST
            will_exceed_max = (
                len(user_reviews_to_request)
                + len(team_reviews_to_request)
                + requested_reviews_nb
                > self.GITHUB_MAXIMUM_REVIEW_REQUEST
            )

            if not already_at_max:
                try:
                    ctxt.client.post(
                        f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/requested_reviewers",
                        json={
                            "reviewers": list(user_reviews_to_request),
                            "team_reviewers": list(team_reviews_to_request),
                        },
                    )
                except http.HTTPClientSideError as e:  # pragma: no cover
                    return check_api.Result(
                        check_api.Conclusion.PENDING,
                        "Unable to create review request",
                        f"GitHub error: [{e.status_code}] `{e.message}`",
                    )
            if already_at_max or will_exceed_max:
                return check_api.Result(
                    check_api.Conclusion.NEUTRAL,
                    "Maximum number of reviews already requested",
                    f"The maximum number of {self.GITHUB_MAXIMUM_REVIEW_REQUEST} reviews has been reached.\n"
                    "Unable to request reviews for additional users.",
                )

            return check_api.Result(
                check_api.Conclusion.SUCCESS, "New reviews requested", ""
            )
        else:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "No new reviewers to request", ""
            )
