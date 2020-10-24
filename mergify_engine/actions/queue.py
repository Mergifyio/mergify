# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import dataclasses
import typing

import daiquiri
import voluptuous

from mergify_engine import context
from mergify_engine import queue
from mergify_engine.actions import merge_base


if typing.TYPE_CHECKING:
    from mergify_engine import rules

LOG = daiquiri.getLogger(__name__)


class QueueAction(merge_base.MergeBaseAction):
    validator = {
        voluptuous.Required("name"): str,
        voluptuous.Required(
            "priority", default=merge_base.PriorityAliases.medium.value
        ): voluptuous.All(
            voluptuous.Any("low", "medium", "high", int),
            voluptuous.Coerce(merge_base.Priority),
            int,
            voluptuous.Range(min=1, max=10000),
        ),
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "merge",
        ),
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
    }

    queue_rule: "rules.QueueRule" = dataclasses.field(init=False)

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        self.config["bot_account"] = None
        self.config["update_bot_account"] = None
        self.config["strict"] = "smart+ordered"

        # This will not be used for batching pull request
        self.config["strict_method"] = "merge"

        # TODO(sileht): Only merge is currently supported
        self.config["rebase_fallback"] = None

        for rule in mergify_config["queue_rules"]:
            if rule.name == self.config["name"]:
                self.queue_rule = rule
                self.config.update(rule.config)
                break
        else:
            raise voluptuous.error.Invalid(f"{self.config['name']} queue not found")

    def _should_be_queued(self, ctxt: context.Context, q: queue.Queue) -> bool:
        queue_rule = self.queue_rule.get_pull_request_rule(ctxt)

        # NOTE(sileht): How to decide to keep a PR in queue, user can set conditions only
        # on ending state of a check, so:
        # * we pick not yet validated checks conditions (we don't care that the user
        #   assert on failure/neutral/success).
        # * We remove from missing conditions those where the check is pending
        # * If we still have missing conditions, this means a check have reached a
        # ending state but not the one expected by the user, so we can't merge the PR

        missing_checks_conditions = [
            condition
            for condition in queue_rule.missing_conditions
            if condition.attribute_name.startswith("check-")
            or condition.attribute_name.startswith("status-")
        ]
        if not missing_checks_conditions:
            return True

        states_of_missing_checks = [
            state
            for name, state in ctxt.checks.items()
            for cond in missing_checks_conditions
            if cond(**{cond.attribute_name: name})
        ]
        #  We have missing conditions but no associated states, this means
        #  that some checks are missing, we assume they are pending
        if not states_of_missing_checks:
            return True

        for state in states_of_missing_checks:
            # We found a missing condition with the check pending, keep the PR
            # in queue
            if state in ("pending", None):
                return True

        # We don't have any checks pending, but some conditions still don't match,
        # so can never ever merge this PR, removing it from the queue
        return False

    def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        return True

    def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        # TODO(sileht): compute EvaluatedQueueRule only once per action run
        queue_rule = self.queue_rule.get_pull_request_rule(ctxt)
        return (
            q.is_first_pull(ctxt)
            and not queue_rule.missing_conditions
            and not ctxt.is_behind
        )

    def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        queue_rule = self.queue_rule.get_pull_request_rule(ctxt)
        return (
            q.is_first_pull(ctxt)
            and not queue_rule.missing_conditions
            and ctxt.is_behind
        )

    def get_merge_conditions(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
    ) -> typing.Tuple["rules.RuleConditions", "rules.RuleMissingConditions"]:
        queue_rule = self.queue_rule.get_pull_request_rule(ctxt)
        return queue_rule.conditions, queue_rule.missing_conditions
