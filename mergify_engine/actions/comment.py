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
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import http
from mergify_engine.rules import types


class CommentAction(actions.Action):
    validator = {
        voluptuous.Required("message", default=None): types.Jinja2WithNone,
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
    }

    silent_report = True

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        if self.config["message"] is None:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Message is not set", ""
            )

        bot_account_result = await action_utils.validate_bot_account(
            ctxt,
            self.config["bot_account"],
            required_feature=subscription.Features.BOT_ACCOUNT,
            missing_feature_message="Comments with `bot_account` set are disabled",
            required_permissions=[],
        )
        if bot_account_result is not None:
            return bot_account_result

        try:
            message = await ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid comment message",
                str(rmf),
            )

        if bot_account := self.config["bot_account"]:
            user_tokens = await ctxt.repository.installation.get_user_tokens()
            github_user = user_tokens.get_token_for(bot_account)
            if not github_user:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to comment: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )
        else:
            github_user = None

        try:
            await ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                oauth_token=github_user["oauth_access_token"] if github_user else None,
                json={"body": message},
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.PENDING,
                "Unable to post comment",
                f"GitHub error: [{e.status_code}] `{e.message}`",
            )
        await signals.send(
            ctxt, "action.comment", {"bot_account": bool(self.config["bot_account"])}
        )
        return check_api.Result(check_api.Conclusion.SUCCESS, "Comment posted", message)
