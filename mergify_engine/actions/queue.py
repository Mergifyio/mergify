# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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
from first import first
import voluptuous

from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import queue
from mergify_engine import subscription
from mergify_engine.actions import merge_base
from mergify_engine.rules import types


if typing.TYPE_CHECKING:
    from mergify_engine import rules


LOG = daiquiri.getLogger(__name__)


class QueueAction(merge_base.MergeBaseAction):
    validator = {
        voluptuous.Required("name"): str,
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("merge_bot_account", default=None): voluptuous.Any(
            None, types.GitHubLogin
        ),
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
        voluptuous.Required(
            "priority", default=merge_base.PriorityAliases.medium.value
        ): merge_base.PrioritySchema,
    }

    # NOTE(sileht): We use the max priority as an offset to order queue
    QUEUE_PRIORITY_OFFSET: int = merge_base.MAX_PRIORITY

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        if not ctxt.subscription.has_feature(subscription.Features.QUEUE_ACTION):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue action is disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )
        return await super().run(ctxt, rule)

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        self.config["bot_account"] = None
        self.config["update_bot_account"] = None
        self.config["strict"] = merge_base.StrictMergeParameter.ordered

        for rule in mergify_config["queue_rules"]:
            if rule.name == self.config["name"]:
                self.queue_rule = rule
                break
        else:
            raise voluptuous.error.Invalid(f"{self.config['name']} queue not found")

    def _compute_priority(self) -> int:
        return typing.cast(
            int,
            self.config["priority"]
            + self.queue_rule.config["priority"] * self.QUEUE_PRIORITY_OFFSET,
        )

    async def _should_be_queued(self, ctxt: context.Context) -> bool:
        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        return not check or check_api.Conclusion(check["conclusion"]) in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.PENDING,
        ]

    async def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        if not await q.is_first_pull(ctxt):
            return False

        if not await ctxt.is_behind:
            # TODO(sileht): Even if we merged the pull request here, the train car may
            # have been created. We should postpone the train car.
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(ctxt)
            if not queue_rule_evaluated.missing_conditions:
                return True

        check = first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )
        if check:
            return (
                check_api.Conclusion(check["conclusion"])
                == check_api.Conclusion.SUCCESS
            )
        return False

    async def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        # NOTE(sileht): since we create dedicated branch we don't need to sync PR
        return False

    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        return True

    async def _get_queue(self, ctxt: context.Context) -> queue.Queue:
        return await queue.Queue.from_context(ctxt, with_train=True)

    def get_merge_conditions(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
    ) -> typing.Tuple["rules.RuleConditions", "rules.RuleMissingConditions"]:
        # TODO(sileht): A bit wrong, we should use the context of the testing PR instead
        # of the current one
        return rule.conditions, rule.missing_conditions
