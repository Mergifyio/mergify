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

import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.rules import types


class CommentAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )
    validator = {
        voluptuous.Required("message", default=None): types.Jinja2WithNone,
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        if self.config["message"] is None:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Message is not set", ""
            )

        try:
            bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["bot_account"],
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message="Comments with `bot_account` set are disabled",
                required_permissions=[],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        try:
            message = await ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid comment message",
                str(rmf),
            )

        github_user: typing.Optional[user_tokens.UserTokensUser] = None
        if bot_account:
            tokens = await ctxt.repository.installation.get_user_tokens()
            github_user = tokens.get_token_for(bot_account)
            if not github_user:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to comment: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )

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
            ctxt.repository,
            ctxt.pull["number"],
            "action.comment",
            signals.EventNoMetadata(),
            rule.get_signal_trigger(),
        )
        return check_api.Result(check_api.Conclusion.SUCCESS, "Comment posted", message)

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
