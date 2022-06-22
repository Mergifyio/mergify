# -*- encoding: utf-8 -*-
#
#  Copyright © 2020–2021 Mergify SAS
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

from mergify_engine import actions
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.rules import conditions


class UpdateAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_AS_COMMAND
        | actions.ActionFlag.ALWAYS_RUN
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
    )
    validator: typing.ClassVar[typing.Dict[typing.Any, typing.Any]] = {}

    @staticmethod
    async def run(ctxt: context.Context, rule: rules.EvaluatedRule) -> check_api.Result:
        try:
            await branch_updater.update_with_api(ctxt)
        except branch_updater.BranchUpdateFailure as e:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                e.title,
                e.message,
            )
        else:
            await signals.send(
                ctxt.repository,
                ctxt.pull["number"],
                "action.update",
                signals.EventNoMetadata(),
                rule.get_signal_trigger(),
            )
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Branch has been successfully updated",
                "",
            )

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        description = ":pushpin: update requirement"
        return [
            conditions.RuleCondition(
                "-closed",
                description=description,
            ),
            conditions.RuleCondition(
                "#commits-behind>0",
                description=description,
            ),
        ]

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
