# -*- encoding: utf-8 -*-
#
#  Copyright © 2021 Mergify SAS
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
from mergify_engine.actions import utils as action_utils
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.rules import types


class DraftAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_RUN
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )

    validator = {
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if ctxt.pull["draft"]:
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Pull request is already a draft.",
                "",
            )
        try:
            bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["bot_account"],
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message="Draft with `bot_account` set is disabled",
                required_permissions=[],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)
        tokens = await user_tokens.UserTokens.select_users_for(ctxt, bot_account)
        if not tokens or tokens[0]["oauth_access_token"] is None:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                f"Unable to set draft with user `{tokens[0]['login']}`",
                f"Please make sure `{tokens[0]['login']}` has logged in Mergify dashboard.",
            )
        mutation = f"""
            mutation {{
                convertPullRequestToDraft(input:{{pullRequestId: "{ctxt.pull['node_id']}"}}) {{
                    pullRequest {{
                        isDraft
                    }}
                }}
            }}
        """
        response = (
            await ctxt.client.post(
                "/graphql",
                json={"query": mutation},
                oauth_token=tokens[0]["oauth_access_token"],
            )
        ).json()
        if response["data"] is None:
            ctxt.log.error(
                "GraphQL API call failed, unable to convert PR to draft.",
                response=response,
            )
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "GraphQL API call failed, pull request wasn't converted to draft.",
                "",
            )
        ctxt.pull["draft"] = True
        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            "Pull request successfully converted to draft",
            "",
        )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
