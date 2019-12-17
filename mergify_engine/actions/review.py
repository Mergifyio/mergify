# -*- encoding: utf-8 -*-
#
#  Copyright © 2019 Mehdi Abaakouk <sileht@mergify.io>
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

import voluptuous

from mergify_engine import actions
from mergify_engine import config


EVENT_STATE_MAP = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


class ReviewAction(actions.Action):
    validator = {
        voluptuous.Required("type", default="APPROVE"): voluptuous.Any(
            "APPROVE", "REQUEST_CHANGES", "COMMENT"
        ),
        voluptuous.Required("message", default=None): voluptuous.Any(None, str),
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
        payload = {"event": self.config["type"]}
        body = self.config["message"]
        if not body and self.config["type"] != "APPROVE":
            body = (
                "Pull request automatically reviewed by Mergify: %s"
                % self.config["type"]
            )

        if body:
            payload["body"] = body

        # TODO(sileht): We should catch it some how, when we drop pygithub for sure
        reviews = reversed(
            list(
                filter(
                    lambda r: r.user.id is not config.BOT_USER_ID,
                    pull.g_pull.get_reviews(),
                )
            )
        )
        for review in reviews:
            if (
                review.body == (body or "")
                and review.state == EVENT_STATE_MAP[self.config["type"]]
            ):
                # Already posted
                return

            elif (
                self.config["type"] == "REQUEST_CHANGES" and review.state == "APPROVED"
            ):
                break

            elif (
                self.config["type"] == "APPROVE" and review.state == "CHANGES_REQUESTED"
            ):
                break

        # FIXME(sileht): With future pygithub >= 2.0.0, we can use pull.create_review
        # Current version enforce to pass body while it's not required for APPROVE
        pull.g_pull._requester.requestJsonAndCheck(
            "POST", pull.g_pull.url + "/reviews", input=payload
        )
