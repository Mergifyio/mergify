#
# Copyright © 2020 Mergify SAS
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import enum
import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.actions import merge_base
from mergify_engine.actions import utils as action_utils
from mergify_engine.dashboard import subscription
from mergify_engine.rules import conditions
from mergify_engine.rules import types


# NOTE(sileht): Sentinel object (eg: `marker = object()`) can't be expressed
# with typing yet use the proposed workaround instead:
#   https://github.com/python/typing/issues/689
#   https://www.python.org/dev/peps/pep-0661/
class _UnsetMarker(enum.Enum):
    _MARKER = 0


UnsetMarker: typing.Final = _UnsetMarker._MARKER


def DeprecatedOption(
    message: str,
    default: typing.Any,
) -> typing.Callable[[typing.Any], typing.Any]:
    def validator(v: typing.Any) -> typing.Any:
        if v is UnsetMarker:
            return default
        else:
            raise voluptuous.Invalid(message % v)

    return validator


class MergeAction(merge_base.MergeBaseAction[None]):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
        | actions.ActionFlag.SUCCESS_IS_FINAL_STATE
        # FIXME(sileht): MRGFY-562
        # enforce -merged/-closed in conditions requirements
        # | actions.ActionFlag.ALWAYS_RUN
    )

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase",
            "merge",
            "squash",
            "fast-forward",
        ),
        # NOTE(sileht): None is supported for legacy reason
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", "none", None
        ),
        voluptuous.Required("merge_bot_account", default=None): types.Jinja2WithNone,
        voluptuous.Required(
            "commit_message_template", default=None
        ): types.Jinja2WithNone,
        voluptuous.Required(
            "priority", default=queue.PriorityAliases.medium.value
        ): queue.PrioritySchema,
    }

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        try:
            merge_bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["merge_bot_account"],
                option_name="merge_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Cannot use `merge_bot_account` with merge action",
                # NOTE(sileht): we don't allow admin, because if branch protection are
                # enabled, but not enforced on admins, we may bypass them
                required_permissions=["write", "maintain"],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        if self.config["method"] == "fast-forward":
            if self.config["commit_message_template"] is not None:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Commit message can't be changed with fast-forward merge method",
                    "`commit_message_template` must not be set if `method: fast-forward` is set.",
                )

        report = await self.merge_report(ctxt, merge_bot_account)
        if report is not None:
            return report

        return await self._merge(ctxt, rule, None, merge_bot_account)

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        return actions.CANCELLED_CHECK_REPORT

    async def get_queue_status(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        queue: None,
    ) -> check_api.Result:
        title = "The pull request will be merged soon"
        summary = ""
        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    async def send_signal(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", queue: None
    ) -> None:
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.merge",
            signals.EventNoMetadata({}),
            rule.get_signal_trigger(),
        )

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        conditions_requirements: typing.List[
            typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
        ] = []
        if self.config["method"] == "fast-forward":
            conditions_requirements.append(
                conditions.RuleCondition(
                    "#commits-behind=0",
                    description=":pushpin: fast-forward merge requirement",
                )
            )
        conditions_requirements.extend(
            await conditions.get_branch_protection_conditions(
                ctxt.repository, ctxt.pull["base"]["ref"], strict=True
            )
        )
        conditions_requirements.extend(await conditions.get_depends_on_conditions(ctxt))
        return conditions_requirements
