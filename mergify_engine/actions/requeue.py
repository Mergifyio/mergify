# -*- encoding: utf-8 -*-
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
from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import utils


class RequeueAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_COMMAND
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
    )

    validator: typing.ClassVar[typing.Dict[typing.Any, typing.Any]] = {}

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        if not check:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                title="This pull request head commit has not been previously disembarked from queue.",
                summary="",
            )

        if check_api.Conclusion(check["conclusion"]) in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.NEUTRAL,
            check_api.Conclusion.PENDING,
        ]:
            return check_api.Result(
                check_api.Conclusion.NEUTRAL,
                title="This pull request is already queued",
                summary="",
            )

        await check_api.set_check_run(
            ctxt,
            constants.MERGE_QUEUE_SUMMARY_NAME,
            check_api.Result(
                check_api.Conclusion.NEUTRAL,
                "This pull request can be re-embarked automatically",
                "",
            ),
        )

        # NOTE(sileht): refresh it to maybe, retrigger the queue action.
        await utils.send_pull_refresh(
            ctxt.redis.stream,
            ctxt.pull["base"]["repo"],
            pull_request_number=ctxt.pull["number"],
            action="user",
            source="action/command/requeue",
        )

        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.requeue",
            signals.EventNoMetadata(),
            rule.get_signal_trigger(),
        )

        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            title="The queue state of this pull request has been cleaned. It can be re-embarked automatically",
            summary="",
        )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
