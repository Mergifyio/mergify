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

import daiquiri
import voluptuous

from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine.actions import merge_base
from mergify_engine.queue import naive
from mergify_engine.rules import types


LOG = daiquiri.getLogger(__name__)


class MergeAction(merge_base.MergeBaseAction):
    MESSAGE_ACTION_NAME = "Merge"

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        # NOTE(sileht): None is supported for legacy reason
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", "none", None
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
        voluptuous.Required("merge_bot_account", default=None): types.Jinja2WithNone,
        voluptuous.Required("update_bot_account", default=None): types.Jinja2WithNone,
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
        voluptuous.Required(
            "priority", default=merge_base.PriorityAliases.medium.value
        ): merge_base.PrioritySchema,
    }

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        self.config["queue_config"] = rules.QueueConfig(
            {
                "priority": 0,
                "speculative_checks": 1,
                "batch_size": 1,
                "allow_inplace_speculative_checks": True,
            }
        )

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        if self.config[
            "priority"
        ] != merge_base.PriorityAliases.medium.value and not ctxt.subscription.has_feature(
            subscription.Features.PRIORITY_QUEUES
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Merge with `priority` set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )
        q = await naive.Queue.from_context(ctxt)
        return await self._run(ctxt, rule, q)

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        q = await naive.Queue.from_context(ctxt)
        return await self._cancel(ctxt, rule, q)

    async def _should_be_synced(self, ctxt: context.Context, q: queue.QueueT) -> bool:
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

    async def _should_be_queued(self, ctxt: context.Context, q: queue.QueueT) -> bool:
        return True

    async def _should_be_merged(self, ctxt: context.Context, q: queue.QueueT) -> bool:
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

    async def _should_be_merged_during_cancel(
        self, ctxt: context.Context, q: queue.QueueBase
    ) -> bool:
        # NOTE(sileht): we prefer wait the engine to be retriggered and rerun run()
        return False

    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.QueueBase
    ) -> bool:
        # It's closed, it's not going to change
        if ctxt.closed:
            return True

        if await ctxt.has_been_synchronized_by_user():
            return True

        position = await q.get_position(ctxt)
        if position is None:
            return True

        pull_rule_checks_status = await merge_base.get_rule_checks_status(
            ctxt.log, [ctxt.pull_request], rule
        )
        return pull_rule_checks_status == check_api.Conclusion.FAILURE

    async def _get_queue_summary(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.QueueBase
    ) -> str:
        pulls = await q.get_pulls()

        if pulls and ctxt.pull["number"] == pulls[0] and await ctxt.is_behind:
            summary = (
                "The pull request base branch will be updated before being merged.\n\n"
            )
        else:
            summary = ""

        summary += "**Required conditions for merge:**\n\n"
        summary += rule.conditions.get_summary()

        if pulls:
            table = [
                "| | Pull request | Priority |",
                "| ---: | :--- | :--- |",
            ]
            for i, pull_number in enumerate(pulls):
                # TODO(sileht): maybe cache pull request title to avoid this lookup
                q_pull_ctxt = await ctxt.repository.get_pull_request_context(
                    pull_number
                )
                config = await q.get_config(pull_number)
                try:
                    fancy_priority = merge_base.PriorityAliases(config["priority"]).name
                except ValueError:
                    fancy_priority = str(config["priority"])

                table.append(
                    f"| {i + 1} "
                    f"| {q_pull_ctxt.pull['title']} #{pull_number} "
                    f"| {fancy_priority} "
                    "|"
                )

            summary += (
                "\n**The following pull requests are queued:**\n"
                + "\n".join(table)
                + "\n"
            )

        summary += "\n---\n\n"
        summary += constants.MERGIFY_PULL_REQUEST_DOC
        return summary

    async def send_signal(self, ctxt: context.Context) -> None:
        await signals.send(
            ctxt,
            "action.merge",
            {
                "merge_bot_account": bool(self.config["merge_bot_account"]),
                "update_bot_account": bool(self.config["update_bot_account"]),
                "strict": self.config["strict"].value,
            },
        )
