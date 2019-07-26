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
        pull.g_pull.create_review_request(self.config['users'])
