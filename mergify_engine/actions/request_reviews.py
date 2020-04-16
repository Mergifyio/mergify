import itertools

import httpx
import voluptuous

from mergify_engine import actions


class RequestReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("users", default=[]): [str],
        voluptuous.Required("teams", default=[]): [str],
    }

    silent_report = True

    always_run = True

    def run(self, ctxt, missing_conditions):

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
            try:
                ctxt.client.post(
                    f"pulls/{ctxt.pull['number']}/requested_reviewers",
                    json={
                        "reviewers": list(user_reviews_to_request),
                        "team_reviewers": list(team_reviews_to_request),
                    },
                )
            except httpx.HTTPClientSideError as e:  # pragma: no cover
                return (
                    None,
                    "Unable to create review request",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )
            return ("success", "New reviews requested", "")
        else:
            return ("success", "No new reviewers to request", "")
