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
from mergify_engine.rules import types


BOT_ACCOUNT_DEPRECATION_NOTICE = """This pull request has been rebased with the
configuration option `bot_account` discontinued for Open Source plan.

To continue to use this option, you must change your plan on
https://dashboard.mergify.io. On June 1st, 2021, if the `bot_account` is still
set, the action `rebase` will stop working for Open Source plan.
"""


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

            if self.config[
                "bot_account"
            ] is not None and not ctxt.subscription.has_feature(
                subscription.Features.BOT_ACCOUNT
            ):
                ctxt.log.info("rebase bot_account used by free plan")

            try:
                await branch_updater.rebase_with_git(ctxt, self.config["bot_account"])
            except branch_updater.BranchUpdateFailure as e:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, e.title, e.message
                )

            except branch_updater.AuthenticationFailure as e:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, "Branch rebase failed", str(e)
                )

            if self.config[
                "bot_account"
            ] is not None and not ctxt.subscription.has_feature(
                subscription.Features.BOT_ACCOUNT
            ):
                await ctxt.client.post(
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                    json={"body": BOT_ACCOUNT_DEPRECATION_NOTICE},
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
