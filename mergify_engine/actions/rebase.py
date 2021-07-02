# -*- encoding: utf-8 -*-
#
#  Copyright © 2019–2021 Mergify SAS
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
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine.actions import utils as action_utils
from mergify_engine.rules import types


class RebaseAction(actions.Action):
    is_command = True

    always_run = True

    silent_report = True

    validator = {
        voluptuous.Required("bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
    }

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if not config.GITHUB_APP:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Unavailable with GitHub Action",
                "Due to GitHub Action limitation, the `rebase` command is only available "
                "with the Mergify GitHub App.",
            )

        if await ctxt.is_behind:
            repo_info = await ctxt.client.item(ctxt.pull["base"]["repo"]["url"])

            if repo_info[
                "size"
            ] > config.NOSUB_MAX_REPO_SIZE_KB and not ctxt.subscription.has_feature(
                subscription.Features.LARGE_REPOSITORY
            ):
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Branch rebase failed",
                    f"Your repository is above {config.NOSUB_MAX_REPO_SIZE_KB} KB.\n{ctxt.subscription.missing_feature_reason(ctxt.pull['base']['repo']['owner']['login'])}",
                )

            bot_account_result = await action_utils.validate_bot_account(
                ctxt,
                self.config["bot_account"],
                option_name="bot_account",
                required_feature=subscription.Features.BOT_ACCOUNT,
                missing_feature_message="Rebase with `update_bot_account` set is unavailable",
            )
            if bot_account_result is not None:
                return bot_account_result

            try:
                await branch_updater.rebase_with_git(ctxt, self.config["bot_account"])
            except branch_updater.BranchUpdateFailure as e:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, e.title, e.message
                )

            await signals.send(
                ctxt, "action.rebase", {"bot_account": bool(self.config["bot_account"])}
            )
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Branch has been successfully rebased",
                "",
            )
        else:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Branch already up to date", ""
            )
