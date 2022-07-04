# -*- encoding: utf-8 -*-
#
#  Copyright © 2021–2022 Mergify SAS
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
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import github
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.rules import types


class EditAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_RUN
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )

    validator = {
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
        voluptuous.Required("draft", default=None): voluptuous.Any(None, bool),
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if self.config["draft"] is not None:
            return await self._edit_draft_state(ctxt, rule, self.config["draft"])
        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            "Nothing to do.",
            "",
        )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT

    async def _edit_draft_state(
        self,
        ctxt: context.Context,
        rule: rules.EvaluatedRule,
        draft_converted: bool,
    ) -> check_api.Result:

        if draft_converted:
            expected_state = True
            current_state = "draft"
            mutation = "convertPullRequestToDraft"
        else:
            expected_state = False
            current_state = "ready for review"
            mutation = "markPullRequestReadyForReview"

        if ctxt.pull["draft"] == expected_state:
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                f"Pull request is already {current_state}.",
                "",
            )

        try:
            bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["bot_account"],
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message=f"{current_state} with `bot_account` set is disabled",
                required_permissions=[],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        try:
            tokens = await user_tokens.UserTokens.select_users_for(ctxt, bot_account)
        except user_tokens.UserTokensUserNotFound as e:
            return check_api.Result(
                check_api.Conclusion.FAILURE, "Fail to convert pull request", e.reason
            )

        mutation = f"""
            mutation {{
                {mutation}(input:{{pullRequestId: "{ctxt.pull['node_id']}"}}) {{
                    pullRequest {{
                        isDraft
                    }}
                }}
            }}
        """
        try:
            await ctxt.client.graphql_post(
                mutation,
                oauth_token=tokens[0]["oauth_access_token"],
            )
        except github.GraphqlError as e:
            if "Field 'convertPullRequestToDraft' doesn't exist" in e.message:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Converting pull request to draft requires GHES >= 3.2",
                    "",
                )
            ctxt.log.error(
                "GraphQL API call failed, unable to convert PR.",
                current_state=current_state,
                response=e.message,
            )
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                f"GraphQL API call failed, pull request wasn't converted to {current_state}.",
                "",
            )
        ctxt.pull["draft"] = expected_state

        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.edit",
            signals.EventEditMetadata({"draft": expected_state}),
            rule.get_signal_trigger(),
        )

        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            f"Pull request successfully converted to {current_state}",
            "",
        )
