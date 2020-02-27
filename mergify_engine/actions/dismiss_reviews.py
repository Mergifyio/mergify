# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import github
import voluptuous

from mergify_engine import actions
from mergify_engine import config
from mergify_engine import utils


class DismissReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("approved", default=True): voluptuous.Any(
            True, False, [str]
        ),
        voluptuous.Required("changes_requested", default=True): voluptuous.Any(
            True, False, [str]
        ),
        voluptuous.Required("message", default="Pull request has been modified."): str,
    }

    always_run = True

    silent_report = True

    @staticmethod
    def _have_been_synchronized(sources):
        for source in sources:
            if (
                source["event_type"] == "pull_request"
                and source["data"]["action"] == "synchronize"
                and source["data"]["sender"]["id"] != config.BOT_USER_ID
            ):
                return True
        return False

    def run(self, pull, sources, missing_conditions):
        if self._have_been_synchronized(sources):
            # FIXME(sileht): Currently sender id is not the bot by the admin
            # user that enroll the repo in Mergify, because branch_updater uses
            # his access_token instead of the Mergify installation token.
            # As workaround we track in redis merge commit id
            # This is only true for method="rebase"
            redis = utils.get_redis_for_cache()
            if redis.get("branch-update-%s" % pull.head_sha):
                return

            for review in pull.to_dict()["_approvals"]:
                conf = self.config.get(review.state.lower(), False)
                if conf and (conf is True or review.user.login in conf):
                    self._dismissal_review(pull, review)

    def _dismissal_review(self, pull, review):
        try:
            review._requester.requestJsonAndCheck(
                "PUT",
                "%s/reviews/%s/dismissals" % (review.pull_request_url, review.id),
                input={"message": self.config["message"]},
                headers={"Accept": "application/vnd.github.machine-man-preview+json"},
            )
        except github.GithubException as e:  # pragma: no cover
            if e.status >= 500:
                raise
            return (
                None,
                "Unable to dismiss review",
                f"GitHub error: [{e.status_code}] `{e.data['message']}`",
            )
        return ("success", "Review dismissed", "")
