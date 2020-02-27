import itertools

import github
import voluptuous

from mergify_engine import actions


class RequestReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("users", default=[]): [str],
        voluptuous.Required("teams", default=[]): [str],
    }

    silent_report = True

    def run(self, pull, sources, missing_conditions):

        # Using consolidated data to avoid already done API lookup
        data = pull.to_dict()
        reviews_keys = (
            "approved-reviews-by",
            "dismissed-reviews-by",
            "changes-requested-reviews-by",
            "commented-reviews-by",
            "review-requested",
        )
        existing_reviews = set(itertools.chain(*[data[key] for key in reviews_keys]))
        user_reviews_to_request = (
            set(self.config["users"]) - existing_reviews - set((pull.user))
        )
        team_reviews_to_request = set(self.config["teams"]).difference(
            # Team starts with @
            {e[1:] for e in existing_reviews if e.startswith("@")}
        )
        if user_reviews_to_request or team_reviews_to_request:
            try:
                pull.g_pull.create_review_request(
                    reviewers=list(user_reviews_to_request),
                    team_reviewers=list(team_reviews_to_request),
                )
            except github.GithubException as e:  # pragma: no cover
                if e.status >= 500:
                    raise
                return (
                    None,
                    "Unable to create review request",
                    f"GitHub error: [{e.status}] `{e.data['message']}`",
                )
            return ("success", "New reviews requested", "")
        else:
            return ("success", "No new reviewers to request", "")
