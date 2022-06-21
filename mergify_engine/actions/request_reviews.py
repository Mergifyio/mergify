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
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import utils
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.rules import types


ReviewEntityWithWeightT = typing.Union[
    typing.Dict[types.GitHubLogin, int], typing.Dict[types.GitHubTeam, int]
]
ReviewEntityT = typing.Union[
    typing.List[types.GitHubLogin],
    typing.List[types.GitHubTeam],
]


def _ensure_weight(entities: ReviewEntityT) -> ReviewEntityWithWeightT:
    return {entity: 1 for entity in entities}


class RequestReviewsAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_RUN
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )

    # This is the maximum number of review you can request on a PR.
    # It's not documented in the API, but it is shown in GitHub UI.
    # Any review passed that number is ignored by GitHub API.
    GITHUB_MAXIMUM_REVIEW_REQUEST = 15

    _random_weight = voluptuous.Required(
        voluptuous.All(int, voluptuous.Range(min=1, max=65535)), default=1
    )

    validator = {
        voluptuous.Required("users", default=list): voluptuous.Any(
            voluptuous.All(
                [types.GitHubLogin],
                voluptuous.Coerce(_ensure_weight),
            ),
            {
                types.GitHubLogin: _random_weight,
            },
        ),
        voluptuous.Required("teams", default=list): voluptuous.Any(
            voluptuous.All(
                [types.GitHubTeam],
                voluptuous.Coerce(_ensure_weight),
            ),
            {
                types.GitHubTeam: _random_weight,
            },
        ),
        voluptuous.Required("users_from_teams", default=list): voluptuous.Any(
            voluptuous.All(
                [types.GitHubTeam],
                voluptuous.Coerce(_ensure_weight),
            ),
            {
                types.GitHubTeam: _random_weight,
            },
        ),
        "random_count": voluptuous.All(
            int,
            voluptuous.Range(1, GITHUB_MAXIMUM_REVIEW_REQUEST),
        ),
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
    }

    def _get_random_reviewers(
        self, random_number: int, pr_author: str
    ) -> typing.Set[str]:
        choices = {
            **{user.lower(): weight for user, weight in self.config["users"].items()},
            **{
                f"@{team.team}": weight for team, weight in self.config["teams"].items()
            },
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
        self, pr_id: int, existing_reviews: typing.Set[str], pr_author: str
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
            user_reviews_to_request = set(self.config["users"].keys())
            team_reviews_to_request = {t.team for t in self.config["teams"].keys()}

        user_reviews_to_request -= existing_reviews
        user_reviews_to_request -= {pr_author}

        # Team starts with @
        team_reviews_to_request -= {
            e[1:] for e in existing_reviews if e.startswith("@")
        }

        return user_reviews_to_request, team_reviews_to_request

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
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

        try:
            bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["bot_account"],
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message="Request reviews with `bot_account` set are disabled",
                required_permissions=[],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        github_user: typing.Optional[user_tokens.UserTokensUser] = None
        if bot_account:
            tokens = await ctxt.repository.installation.get_user_tokens()
            github_user = tokens.get_token_for(bot_account)
            if not github_user:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to comment: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
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
            itertools.chain(
                *[await getattr(ctxt.pull_request, key) for key in reviews_keys]
            )
        )

        team_errors = set()
        for team in self.config["teams"].keys():
            try:
                await team.has_read_permission(ctxt)
            except types.InvalidTeam as e:
                team_errors.add(e.details)

        for team, weight in self.config["users_from_teams"].items():
            try:
                await team.has_read_permission(ctxt)
            except types.InvalidTeam as e:
                team_errors.add(e.details)
            else:
                self.config["users"].update(
                    {
                        user: weight
                        for user in await ctxt.repository.installation.get_team_members(
                            team.team
                        )
                    }
                )

        if team_errors:
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Invalid requested teams",
                "\n".join(team_errors),
            )

        user_reviews_to_request, team_reviews_to_request = self._get_reviewers(
            ctxt.pull["id"],
            existing_reviews,
            ctxt.pull["user"]["login"],
        )

        if user_reviews_to_request or team_reviews_to_request:
            requested_reviews_nb = len(
                typing.cast(typing.List[str], await ctxt.pull_request.review_requested)
            )

            already_at_max = requested_reviews_nb == self.GITHUB_MAXIMUM_REVIEW_REQUEST
            will_exceed_max = (
                len(user_reviews_to_request)
                + len(team_reviews_to_request)
                + requested_reviews_nb
                > self.GITHUB_MAXIMUM_REVIEW_REQUEST
            )

            if not already_at_max:
                try:
                    await ctxt.client.post(
                        f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/requested_reviewers",
                        oauth_token=github_user["oauth_access_token"]
                        if github_user
                        else None,
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
                await signals.send(
                    ctxt.repository,
                    ctxt.pull["number"],
                    "action.request_reviewers",
                    signals.EventRequestReviewsMetadata(
                        {
                            "reviewers": list(user_reviews_to_request),
                            "team_reviewers": list(team_reviews_to_request),
                        }
                    ),
                    rule.get_signal_trigger(),
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

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
