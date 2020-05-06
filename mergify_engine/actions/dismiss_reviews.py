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

import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine import config
from mergify_engine import context
from mergify_engine import utils
from mergify_engine.rules import types


class DismissReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("approved", default=True): voluptuous.Any(
            True, False, [str]
        ),
        voluptuous.Required("changes_requested", default=True): voluptuous.Any(
            True, False, [str]
        ),
        voluptuous.Required(
            "message", default="Pull request has been modified."
        ): types.Jinja2,
    }

    always_run = True

    silent_report = True

    @staticmethod
    def _have_been_synchronized(ctxt):
        for source in ctxt.sources:
            if (
                source["event_type"] == "pull_request"
                and source["data"]["action"] == "synchronize"
                and source["data"]["sender"]["id"] != config.BOT_USER_ID
            ):
                return True
        return False

    def run(self, ctxt, missing_conditions):
        if self._have_been_synchronized(ctxt):
            # FIXME(sileht): Currently sender id is not the bot by the admin
            # user that enroll the repo in Mergify, because branch_updater uses
            # his access_token instead of the Mergify installation token.
            # As workaround we track in redis merge commit id
            # This is only true for method="rebase"
            redis = utils.get_redis_for_cache()
            if redis.get("branch-update-%s" % ctxt.pull["head"]["sha"]):
                return ("success", "Rebased/Updated by us, nothing to do", "")

            try:
                message = ctxt.pull_request.render_template(self.config["message"])
            except context.RenderTemplateFailure as rmf:
                return (
                    "failure",
                    "Invalid dismiss reviews message",
                    str(rmf),
                )

            errors = set()
            for review in ctxt.consolidated_reviews[1]:
                conf = self.config.get(review["state"].lower(), False)
                if conf and (conf is True or review["user"]["login"] in conf):
                    try:
                        ctxt.client.put(
                            f"pulls/{ctxt.pull['number']}/reviews/{review['id']}/dismissals",
                            json={"message": message},
                        )
                    except httpx.HTTPClientSideError as e:  # pragma: no cover
                        errors.add(f"GitHub error: [{e.status_code}] `{e.message}`")

            if errors:
                return (None, "Unable to dismiss review", "\n".join(errors))
            else:
                return ("success", "Review dismissed", "")
        else:
            return (
                "success",
                "Nothing to do, pull request have not been synchronized",
                "",
            )
