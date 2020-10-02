# -*- encoding: utf-8 -*-
#
#  Copyright © 2019—2020 Mergify SAS
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
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine.rules import types


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
        voluptuous.Required("message", default=None): voluptuous.Any(
            types.Jinja2, None
        ),
    }

    silent_report = True

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        payload = {"event": self.config["type"]}

        if self.config["message"]:
            try:
                body = ctxt.pull_request.render_template(self.config["message"])
            except context.RenderTemplateFailure as rmf:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, "Invalid review message", str(rmf)
                )
        else:
            body = None

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
                    lambda r: r["user"]["id"] is not config.BOT_USER_ID, ctxt.reviews
                )
            )
        )
        for review in reviews:
            if (
                review["body"] == (body or "")
                and review["state"] == EVENT_STATE_MAP[self.config["type"]]
            ):
                # Already posted
                return check_api.Result(
                    check_api.Conclusion.SUCCESS, "Review already posted", ""
                )

            elif (
                self.config["type"] == "REQUEST_CHANGES"
                and review["state"] == "APPROVED"
            ):
                break

            elif (
                self.config["type"] == "APPROVE"
                and review["state"] == "CHANGES_REQUESTED"
            ):
                break

        ctxt.client.post(
            f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/reviews", json=payload
        )

        return check_api.Result(check_api.Conclusion.SUCCESS, "Review posted", "")
