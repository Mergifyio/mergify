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
from mergify_engine import config
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


class MergeAction(merge_base.MergeBaseAction):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
        | actions.ActionFlag.SUCCESS_IS_FINAL_STATE
        # FIXME(sileht): MRGFY-562
        # enforce -merged/-closed in conditions requirements
        # | actions.ActionFlag.ALWAYS_RUN
    )

    validator_default = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
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

    validator_commit_message_mode = {
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
    }

    @classmethod
    def get_config_schema(
        cls, partial_validation: bool
    ) -> typing.Dict[typing.Any, typing.Any]:
        schema = cls.validator_default.copy()
        if config.ALLOW_COMMIT_MESSAGE_OPTION:
            schema.update(cls.validator_commit_message_mode)
        return schema

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        if not config.ALLOW_COMMIT_MESSAGE_OPTION:
            self.config["commit_message"] = "default"

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        try:
            merge_bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["merge_bot_account"],
                option_name="merge_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Merge with `merge_bot_account` set is unavailable",
                # NOTE(sileht): we don't allow admin, because if branch protection are
                # enabled, but not enforced on admins, we may bypass them
                required_permissions=["write", "maintain"],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        if ctxt.pull["mergeable_state"] == "behind":
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Branch protection setting 'strict' is enabled, and the pull request is not up to date.",
                "",
            )

        report = await self.merge_report(ctxt)
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
        q: typing.Optional[queue.QueueBase],
    ) -> check_api.Result:
        title = "The pull request will be merged soon"
        summary = ""
        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    async def send_signal(self, ctxt: context.Context) -> None:
        await signals.send(
            ctxt,
            "action.merge",
            {
                "merge_bot_account": bool(self.config["merge_bot_account"]),
                "commit_message": self.config["commit_message"],
                "commit_message_set": "commit_message" in self.raw_config,
            },
        )

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        branch_protection_conditions = (
            await conditions.get_branch_protection_conditions(
                ctxt.repository, ctxt.pull["base"]["ref"], strict=True
            )
        )
        depends_on_conditions = await conditions.get_depends_on_conditions(ctxt)
        return branch_protection_conditions + depends_on_conditions
