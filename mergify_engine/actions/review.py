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
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.rules import types


EVENT_STATE_MAP = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


class ReviewAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.ALWAYS_RUN
    )
    validator = {
        voluptuous.Required("type", default="APPROVE"): voluptuous.Any(
            "APPROVE", "REQUEST_CHANGES", "COMMENT"
        ),
        voluptuous.Required("message", default=None): types.Jinja2WithNone,
        voluptuous.Required("bot_account", default=None): types.Jinja2WithNone,
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        payload = {"event": self.config["type"]}

        try:
            bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["bot_account"],
                option_name="bot_account",
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message="Cannot use `bot_account` with review action",
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        if ctxt.pull["merged"] and self.config["type"] != "COMMENT":
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Pull request has been merged, APPROVE and REQUEST_CHANGES are ignored.",
                "",
            )

        if bot_account:
            review_user = bot_account
            user_tokens = await ctxt.repository.installation.get_user_tokens()
            github_user = user_tokens.get_token_for(bot_account)
            if not github_user:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to review: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )
        else:
            review_user = github_types.GitHubLogin(config.BOT_USER_LOGIN)
            github_user = None

        if self.config["message"]:
            try:
                payload["body"] = await ctxt.pull_request.render_template(
                    self.config["message"]
                )
            except context.RenderTemplateFailure as rmf:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, "Invalid review message", str(rmf)
                )
        elif self.config["type"] != "APPROVE":
            payload[
                "body"
            ] = f"Pull request automatically reviewed by Mergify: {self.config['type']}"

        reviews = reversed(
            list(
                filter(
                    lambda r: r["user"]["login"] == review_user,
                    await ctxt.reviews,
                )
            )
        )

        for review in reviews:
            if (
                review["body"] == payload.get("body", "")
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

        try:
            await ctxt.client.post(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/reviews",
                oauth_token=github_user["oauth_access_token"] if github_user else None,
                json=payload,
            )
        except http.HTTPClientSideError as e:
            if e.status_code == 422 and "errors" in e.response.json():
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Review failed",
                    "GitHub returned an unexpected error:\n\n * "
                    + "\n * ".join(f"`{s}`" for s in e.response.json()["errors"]),
                )
            raise

        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.review",
            signals.EventReviewMetadata(
                {"type": self.config["type"], "reviewer": review_user}
            ),
            rule.get_signal_trigger(),
        )
        return check_api.Result(check_api.Conclusion.SUCCESS, "Review posted", "")

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
