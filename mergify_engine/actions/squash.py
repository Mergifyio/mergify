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


import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import squash_pull
from mergify_engine import subscription
from mergify_engine.actions import utils as action_utils
from mergify_engine.rules import types


class SquashAction(actions.Action):
    is_command = True
    always_run = True
    silent_report = True

    validator = {
        voluptuous.Required("bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
        voluptuous.Required("commit_message", default="all-commits"): voluptuous.Any(
            "all-commits", "first-commit", "title+body"
        ),
    }

    @staticmethod
    def command_to_config(string: str) -> typing.Dict[str, typing.Any]:
        if string:
            return {"commit_message": string.strip()}
        else:
            return {}

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        bot_account_result = await action_utils.validate_bot_account(
            ctxt,
            self.config["bot_account"],
            required_feature=subscription.Features.BOT_ACCOUNT,
            missing_feature_message="Squash with `bot_account` set are disabled",
            required_permissions=[],
        )
        if bot_account_result is not None:
            return bot_account_result

        if ctxt.pull["commits"] <= 1:
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Pull request is already one-commit long",
                "",
            )

        try:
            commit_title_and_message = await ctxt.pull_request.get_commit_message()
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Invalid commit message",
                str(rmf),
            )

        if commit_title_and_message is not None:
            title, message = commit_title_and_message
            message = f"{title}\n\n{message}"

        elif self.config["commit_message"] == "all-commits":
            message = f"{(await ctxt.pull_request.title)} (#{(await ctxt.pull_request.number)})"
            message += "\n\n* ".join(
                [commit["commit"]["message"] for commit in await ctxt.commits]
            )

        elif self.config["commit_message"] == "first-commit":
            message = (await ctxt.commits)[0]["commit"]["message"]

        elif self.config["commit_message"] == "title+body":
            message = f"{(await ctxt.pull_request.title)} (#{(await ctxt.pull_request.number)})"
            message += f"\n\n{await ctxt.pull_request.body}"

        else:
            raise RuntimeError("Unsupported commit_message option")

        try:
            await squash_pull.squash(
                ctxt,
                message,
                self.config["bot_account"],
            )
        except squash_pull.SquashFailure as e:
            return check_api.Result(
                check_api.Conclusion.FAILURE, "Pull request squash failed", e.reason
            )
        else:
            await signals.send(ctxt, "action.squash")
        return check_api.Result(
            check_api.Conclusion.SUCCESS, "Pull request squashed successfully", ""
        )
