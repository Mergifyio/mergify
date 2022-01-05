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
from mergify_engine import github_graphql_types
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.clients import http
from mergify_engine.rules import types


class SubscriptionAction(actions.Action):
    validator = {
        voluptuous.Required("subscribed", default=list): [types.Jinja2],
        voluptuous.Required("unsubscribed", default=list): [types.Jinja2],
    }

    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        tokens = await ctxt.repository.installation.get_user_tokens()

        missing_oauth_tokens = set()
        for subscription_type in ("subscribed", "unsubscribed"):
            for user in self.config[subscription_type]:
                github_user = tokens.get_token_for(user)
                if not github_user:
                    missing_oauth_tokens.add(user)
                    continue

                await self.subscribe(
                    ctxt,
                    github_user["oauth_access_token"],
                    typing.cast(
                        github_graphql_types.GraphqlSubscriptionState,
                        subscription_type.upper(),
                    ),
                )

        if missing_oauth_tokens:
            message = (
                f"Unable to subscribe some users: {', '.join(missing_oauth_tokens)}.\n"
                "Please make sure they have logged in Mergify dashboard."
            )
            try:
                await ctxt.client.post(
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                    json={"body": message},
                )
            except http.HTTPClientSideError:  # pragma: no cover
                ctxt.log.warning("fail to inform about missing oauth access token")

        return check_api.Result(check_api.Conclusion.SUCCESS, "Subscription done", "")

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT

    async def subscribe(
        self,
        ctxt: context.Context,
        oauth_access_token: github_types.GitHubOAuthToken,
        state: github_graphql_types.GraphqlSubscriptionState,
    ) -> None:
        mutation = f"""
            mutation {{
               updateSubscription(input:{{subscribableId: "{ctxt.pull['node_id']}", state: {state} }}) {{
                    subscribable {{
                        viewerSubscription
                    }}
                }}
            }}
        """
        resp = await ctxt.client.graphql_post(mutation, oauth_token=oauth_access_token)
        ctxt.log.error("SUBSCRIBE", resp=resp)
