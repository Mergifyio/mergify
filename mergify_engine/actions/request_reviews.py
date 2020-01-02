import itertools

import voluptuous

from mergify_engine import actions


class RequestReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("users", default=[]): [str],
        voluptuous.Required("teams", default=[]): [str],
    }

    def run(
        self,
        installation_id,
        installation_token,
        event_type,
        data,
        pull,
        missing_conditions,
    ):

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
            set(self.config["users"]) - existing_reviews - set((pull.g_pull.author,))
        )
        team_reviews_to_request = set(self.config["teams"]).difference(
            # Team starts with @
            {e[1:] for e in existing_reviews if e.startswith("@")}
        )
        if user_reviews_to_request or team_reviews_to_request:
            pull.g_pull.create_review_request(
                reviewers=list(user_reviews_to_request),
                team_reviewers=list(team_reviews_to_request),
            )
