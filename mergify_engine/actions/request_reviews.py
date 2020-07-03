import itertools

import voluptuous

from mergify_engine import actions
from mergify_engine.clients import http
from mergify_engine.rules import types


class RequestReviewsAction(actions.Action):

    # This is the maximum number of review you can request on a PR.
    # It's not documented in the API, but it is shown in GitHub UI.
    # Any review passed that number is ignored by GitHub API.
    GITHUB_MAXIMUM_REVIEW_REQUEST = 15

    validator = {
        voluptuous.Required("users", default=[]): [types.GitHubLogin],
        voluptuous.Required("teams", default=[]): [types.GitHubTeam],
    }

    silent_report = True

    always_run = True

    def run(self, ctxt, rule, missing_conditions):
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
        user_reviews_to_request = (
            set(self.config["users"])
            - existing_reviews
            - set((ctxt.pull["user"]["login"],))
        )
        team_reviews_to_request = set(self.config["teams"]).difference(
            # Team starts with @
            {e[1:] for e in existing_reviews if e.startswith("@")}
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
                        f"pulls/{ctxt.pull['number']}/requested_reviewers",
                        json={
                            "reviewers": list(user_reviews_to_request),
                            "team_reviewers": list(team_reviews_to_request),
                        },
                    )
                except http.HTTPClientSideError as e:  # pragma: no cover
                    return (
                        None,
                        "Unable to create review request",
                        f"GitHub error: [{e.status_code}] `{e.message}`",
                    )
            if already_at_max or will_exceed_max:
                return (
                    "neutral",
                    "Maximum number of reviews already requested",
                    f"The maximum number of {self.GITHUB_MAXIMUM_REVIEW_REQUEST} reviews has been reached.\n"
                    "Unable to request reviews for additional users.",
                )

            return ("success", "New reviews requested", "")
        else:
            return ("success", "No new reviewers to request", "")
