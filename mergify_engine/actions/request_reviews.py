import itertools

import voluptuous

from mergify_engine import actions


class RequestReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("users", default=[]): [str],
        # TODO(sileht): Tests show that github accept this input but nobody is
        # added to review requested... so don't allow it for now
        # voluptuous.Required("teams", default=[]): [str],
    }

    def run(self, installation_id, installation_token, event_type, data,
            pull, missing_conditions):

        # Using consolidated data to avoid already done API lookup
        data = pull.to_dict()
        reviews_keys = ("approved-reviews-by", "dismissed-reviews-by",
                        "changes-requested-reviews-by", "commented-reviews-by")
        existing_reviews = set(itertools.chain(
            *[data[key] for key in reviews_keys]
        ))
        reviews_to_request = set(self.config['users']).difference(
            existing_reviews
        )
        pull.g_pull.create_review_request(list(reviews_to_request))
