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


class MergeAction(merge_base.MergeBaseAction):

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("strict", default=False): voluptuous.Any(
            bool,
            voluptuous.All("smart", voluptuous.Coerce(lambda _: "smart+ordered")),
            voluptuous.All(
                "smart+fastpath", voluptuous.Coerce(lambda _: "smart+fasttrack")
            ),
            "smart+fasttrack",
            "smart+ordered",
        ),
        voluptuous.Required("strict_method", default="merge"): voluptuous.Any(
            "rebase", "merge"
        ),
        # NOTE(sileht): Alias of update_bot_account, it's now undocumented but we have
        # users that use it so, we have to keep it
        voluptuous.Required("bot_account", default=None): voluptuous.Any(
            None, types.GitHubLogin
        ),
        voluptuous.Required("merge_bot_account", default=None): voluptuous.Any(
            None, types.GitHubLogin
        ),
        voluptuous.Required("update_bot_account", default=None): voluptuous.Any(
            None, types.GitHubLogin
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

    def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        if self.config["strict"] == "smart+ordered":
            return ctxt.is_behind and q.is_first_pull(ctxt)
        elif self.config["strict"] == "smart+fasttrack":
            return ctxt.is_behind
        elif self.config["strict"] is True:
            return ctxt.is_behind
        elif self.config["strict"]:
            raise RuntimeError("Unexpected strict")
        else:
            return False

    def _should_be_queued(self, ctxt: context.Context, q: queue.Queue) -> bool:
        return True

    def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        if self.config["strict"] == "smart+ordered":
            return not ctxt.is_behind and q.is_first_pull(ctxt)
        elif self.config["strict"] == "smart+fasttrack":
            return not ctxt.is_behind
        elif self.config["strict"] is True:
            return not ctxt.is_behind
        elif self.config["strict"]:
            raise RuntimeError("Unexpected strict")
        else:
            return True

    def _should_be_cancel(
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
            if not ctxt.checks:
                return False

            states = [
                state
                for name, state in ctxt.checks.items()
                for cond in need_look_at_checks
                if cond({cond.attribute_name: name})
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
