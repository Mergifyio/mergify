# -*- encoding: utf-8 -*-
#
#  Copyright © 2018–2021 Mergify SAS
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
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.clients import http
from mergify_engine.rules import types


class DismissReviewsAction(actions.Action):
    validator = {
        voluptuous.Required("approved", default=True): voluptuous.Any(
            True, False, [types.GitHubLogin]
        ),
        voluptuous.Required("changes_requested", default=True): voluptuous.Any(
            True, False, [types.GitHubLogin]
        ),
        voluptuous.Required(
            "message", default="Pull request has been modified."
        ): types.Jinja2,
    }

    always_run = True

    silent_report = True

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if ctxt.has_been_synchronized():
            # FIXME(sileht): Currently sender id is not the bot by the admin
            # user that enroll the repo in Mergify, because branch_updater uses
            # his access_token instead of the Mergify installation token.
            # As workaround we track in redis merge commit id
            # This is only true for method="rebase"
            if await ctxt.redis.get(f"branch-update-{ctxt.pull['head']['sha']}"):
                return check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    "Updated by Mergify, ignoring",
                    "",
                )

            try:
                message = await ctxt.pull_request.render_template(
                    self.config["message"]
                )
            except context.RenderTemplateFailure as rmf:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Invalid dismiss reviews message",
                    str(rmf),
                )

            errors = set()
            for review in (await ctxt.consolidated_reviews())[1]:
                conf = self.config.get(review["state"].lower(), False)
                if conf and (conf is True or review["user"]["login"] in conf):
                    try:
                        await ctxt.client.put(
                            f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/reviews/{review['id']}/dismissals",
                            json={"message": message},
                        )
                    except http.HTTPClientSideError as e:  # pragma: no cover
                        errors.add(f"GitHub error: [{e.status_code}] `{e.message}`")

            if errors:
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Unable to dismiss review",
                    "\n".join(errors),
                )
            else:
                await signals.send(ctxt, "action.dismiss_reviews")
                return check_api.Result(
                    check_api.Conclusion.SUCCESS, "Review dismissed", ""
                )
        else:
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Nothing to do, pull request have not been synchronized",
                "",
            )
