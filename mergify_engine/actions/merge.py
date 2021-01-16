# -*- encoding: utf-8 -*-
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

import typing

import daiquiri
import voluptuous

from mergify_engine import context
from mergify_engine import queue
from mergify_engine.actions import merge_base
from mergify_engine.rules import types


if typing.TYPE_CHECKING:
    from mergify_engine import rules

LOG = daiquiri.getLogger(__name__)


class FakePR:
    def __init__(self, key: str, value: typing.Any):
        setattr(self, key, value)


class MergeAction(merge_base.MergeBaseAction):

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("strict", default=False): voluptuous.All(
            voluptuous.Any(
                bool, "smart", "smart+fastpath", "smart+fasttrack", "smart+ordered"
            ),
            voluptuous.Coerce(merge_base.strict_merge_parameter),
            merge_base.StrictMergeParameter,
        ),
        voluptuous.Required("strict_method", default="merge"): voluptuous.Any(
            "rebase", "merge"
        ),
        # NOTE(sileht): Alias of update_bot_account, it's now undocumented but we have
        # users that use it so, we have to keep it
        voluptuous.Required("bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
        voluptuous.Required("merge_bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
        voluptuous.Required("update_bot_account", default=None): voluptuous.Any(
            None, types.Jinja2
        ),
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
        voluptuous.Required(
            "priority", default=merge_base.PriorityAliases.medium.value
        ): voluptuous.All(
            voluptuous.Any("low", "medium", "high", int),
            voluptuous.Coerce(merge_base.Priority),
            int,
            voluptuous.Range(min=1, max=10000),
        ),
    }

    async def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        if self.config["strict"] is merge_base.StrictMergeParameter.ordered:
            return await ctxt.is_behind and await q.is_first_pull(ctxt)
        elif self.config["strict"] is merge_base.StrictMergeParameter.fasttrack:
            return await ctxt.is_behind
        elif self.config["strict"] is merge_base.StrictMergeParameter.true:
            return await ctxt.is_behind
        elif self.config["strict"] is merge_base.StrictMergeParameter.false:
            return False
        else:
            raise RuntimeError("Unexpected strict")

    def _should_be_queued(self, ctxt: context.Context, q: queue.Queue) -> bool:
        return True

    async def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        if self.config["strict"] is merge_base.StrictMergeParameter.ordered:
            return not await ctxt.is_behind and await q.is_first_pull(ctxt)
        elif self.config["strict"] is merge_base.StrictMergeParameter.fasttrack:
            return not await ctxt.is_behind
        elif self.config["strict"] is merge_base.StrictMergeParameter.true:
            return not await ctxt.is_behind
        elif self.config["strict"] is merge_base.StrictMergeParameter.false:
            return True
        else:
            raise RuntimeError("Unexpected strict")

    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        # It's closed, it's not going to change
        if ctxt.pull["state"] == "closed":
            return True

        if ctxt.have_been_synchronized():
            return True

        need_look_at_checks = []
        for condition in rule.missing_conditions:
            if condition.attribute_name.startswith(
                "check-"
            ) or condition.attribute_name.startswith("status-"):
                # TODO(sileht): Just return True here, no need to checks checks anymore,
                # this method is no more used by teh merge queue
                need_look_at_checks.append(condition)
            else:
                # something else does not match anymore
                return True

        if need_look_at_checks:
            if not await ctxt.checks:
                return False

            states = [
                state
                for name, state in (await ctxt.checks).items()
                for cond in need_look_at_checks
                if await cond(FakePR(cond.attribute_name, name))
            ]
            if not states:
                return False

            for state in states:
                if state in ("pending", None):
                    return False

        return True

    def get_merge_conditions(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
    ) -> typing.Tuple["rules.RuleConditions", "rules.RuleMissingConditions"]:
        return rule.conditions, rule.missing_conditions
