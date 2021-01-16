# -*- encoding: utf-8 -*-
#
#  Copyright © 2020 Mergify SAS
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
from mergify_engine import subscription
from mergify_engine.clients import http
from mergify_engine.rules import types


class CommentAction(actions.Action):
    validator = {
        voluptuous.Required("message"): types.Jinja2,
        voluptuous.Required("bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
    }

    silent_report = True

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        if self.config["bot_account"] and not ctxt.subscription.has_feature(
            subscription.Features.BOT_ACCOUNT
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Comments with `bot_account` set are disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        try:
            message = await ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid comment message",
                str(rmf),
            )

        if bot_account := self.config["bot_account"]:
            oauth_token = ctxt.subscription.get_token_for(bot_account)
            if not oauth_token:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to comment: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )
        else:
            oauth_token = None

        try:
            await ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                oauth_token=oauth_token,  # type: ignore
                json={"body": message},
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.PENDING,
                "Unable to post comment",
                f"GitHub error: [{e.status_code}] `{e.message}`",
            )
        return check_api.Result(check_api.Conclusion.SUCCESS, "Comment posted", message)
