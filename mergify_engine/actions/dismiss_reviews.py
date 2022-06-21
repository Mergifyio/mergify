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


import logging
import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.clients import http
from mergify_engine.rules import types


WHEN_SYNCHRONIZE = "synchronize"
WHEN_ALWAYS = "always"
FROM_REQUESTED_REVIEWERS = "from_requested_reviewers"

DEFAULT_MESSAGE = {
    WHEN_SYNCHRONIZE: "Pull request has been modified.",
    WHEN_ALWAYS: "Automatic dismiss reviews requested",
}


class DismissReviewsAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.ALWAYS_RUN
    )

    validator = {
        voluptuous.Required("approved", default=True): voluptuous.Any(
            True,
            False,
            [types.GitHubLogin],
            FROM_REQUESTED_REVIEWERS,
        ),
        voluptuous.Required("changes_requested", default=True): voluptuous.Any(
            True,
            False,
            [types.GitHubLogin],
            FROM_REQUESTED_REVIEWERS,
        ),
        voluptuous.Required("message", default=None): types.Jinja2WithNone,
        voluptuous.Required("when", default=WHEN_SYNCHRONIZE): voluptuous.Any(
            WHEN_SYNCHRONIZE, WHEN_ALWAYS
        ),
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        if self.config["message"] is None:
            message_raw = DEFAULT_MESSAGE[self.config["when"]]
        else:
            message_raw = typing.cast(str, self.config["message"])

        try:
            message = await ctxt.pull_request.render_template(message_raw)
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid dismiss reviews message",
                str(rmf),
            )

        if self.config["when"] == WHEN_SYNCHRONIZE and not ctxt.has_been_synchronized():
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Nothing to do, pull request has not been synchronized",
                "",
            )

        # FIXME(sileht): Currently sender id is not the bot by the admin
        # user that enroll the repo in Mergify, because branch_updater uses
        # his access_token instead of the Mergify installation token.
        # As workaround we track in redis merge commit id
        # This is only true for method="rebase"
        if (
            self.config["when"] == WHEN_SYNCHRONIZE
            and not await ctxt.has_been_synchronized_by_user()
        ):
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Updated by Mergify, ignoring", ""
            )

        requested_reviewers_login = {
            rr["login"] for rr in ctxt.pull["requested_reviewers"]
        }

        to_dismiss = set()
        to_dismiss_users = set()
        to_dismiss_user_from_requested_reviewers = set()
        for review in (await ctxt.consolidated_reviews())[1]:
            conf = self.config.get(review["state"].lower(), False)
            if conf is True:
                to_dismiss.add(review["id"])
                to_dismiss_users.add(review["user"]["login"])
            elif conf == FROM_REQUESTED_REVIEWERS:
                if review["user"]["login"] in requested_reviewers_login:
                    to_dismiss.add(review["id"])
                    to_dismiss_users.add(review["user"]["login"])
                    to_dismiss_user_from_requested_reviewers.add(
                        review["user"]["login"]
                    )
            elif isinstance(conf, list):
                if review["user"]["login"] in conf:
                    to_dismiss_users.add(review["user"]["login"])
                    to_dismiss.add(review["id"])

        if not to_dismiss:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Nothing to dismiss", ""
            )

        if (
            self.config.get("approved") == FROM_REQUESTED_REVIEWERS
            and to_dismiss_user_from_requested_reviewers
        ):
            updated_pull = await ctxt.client.item(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}"
            )
            updated_requested_reviewers_login = {
                rr["login"] for rr in updated_pull["requested_reviewers"]
            }
            level = (
                logging.ERROR
                if updated_requested_reviewers_login != requested_reviewers_login
                else logging.INFO
            )
            ctxt.log.log(
                level,
                "about to dismiss approval reviews from requested_reviewers",
                requested_reviewers_login=requested_reviewers_login,
                updated_requested_reviewers_login=updated_requested_reviewers_login,
                to_dismiss_user_from_requested_reviewers=to_dismiss_user_from_requested_reviewers,
            )

        errors = set()
        for review_id in to_dismiss:
            try:
                await ctxt.client.put(
                    f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/reviews/{review_id}/dismissals",
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
            await signals.send(
                ctxt.repository,
                ctxt.pull["number"],
                "action.dismiss_reviews",
                signals.EventDismissReviewsMetadata({"users": list(to_dismiss_users)}),
                rule.get_signal_trigger(),
            )
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Review dismissed", ""
            )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
