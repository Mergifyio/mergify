# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine.actions import copy
from mergify_engine.rules import conditions


class BackportAction(copy.CopyAction):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_AS_COMMAND
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.ALLOW_AS_PENDING_COMMAND
    )

    KIND: duplicate_pull.KindT = "backport"
    HOOK_EVENT_NAME: typing.Literal[
        "action.backport", "action.copy"
    ] = "action.backport"
    BRANCH_PREFIX: str = "bp"
    SUCCESS_MESSAGE: str = "Backports have been created"
    FAILURE_MESSAGE: str = "No backport have been created"

    @staticmethod
    def command_to_config(string: str) -> typing.Dict[str, typing.Any]:
        if string:
            return {"branches": string.split(" ")}
        else:
            return {}

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        return [
            conditions.RuleCondition(
                "merged", description=":pushpin: backport requirement"
            )
        ]
