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

import daiquiri

import github

import voluptuous

from mergify_engine import actions
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


class DismissReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("approved", default=True):
        voluptuous.Any(True, False, [str]),
        voluptuous.Required("changes_requested", default=True):
        voluptuous.Any(True, False, [str])
    }

    def run(self, installation_id, installation_token,
            event_type, data, pull, missing_conditions):
        if (event_type == "pull_request" and data["action"] == "synchronize"):
            # FIXME(sileht): Currently sender id is not the bot by the admin
            # user that enroll the repo in Mergify, because branch_updater uses
            # his access_token instead of the Mergify installation token.
            # As workaround we track in redis merge commit id
            # if data["sender"]["id"] != config.BOT_USER_ID):
            #     return
            redis = utils.get_redis_for_cache()
            if redis.get("branch-update-%s" % pull.g_pull.head.sha):
                return

            for review in pull._get_reviews():
                conf = self.config.get(review.state.lower(), False)
                if conf and (conf is True or review.user.login in conf):
                    self._dismissal_review(pull, review)

    @staticmethod
    def _dismissal_review(pull, review):
        try:
            review._requester.requestJsonAndCheck(
                "PUT",
                "%s/reviews/%s/dismissals" % (review.pull_request_url,
                                              review.id),
                input={'message': "Pull request has been modified."},
                headers={'Accept':
                         'application/vnd.github.machine-man-preview+json'}
            )
        except github.GithubException as e:  # pragma: no cover
            LOG.error("failed to dismiss review", status=e.status,
                      error=e.data["message"], pull_request=pull)
