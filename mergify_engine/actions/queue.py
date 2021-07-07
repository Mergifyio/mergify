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
from mergify_engine import delayed_refresh
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.actions import merge_base
from mergify_engine.actions import utils as action_utils
from mergify_engine.queue import merge_train
from mergify_engine.rules import types


if typing.TYPE_CHECKING:
    from mergify_engine import rules


LOG = daiquiri.getLogger(__name__)


class QueueAction(merge_base.MergeBaseAction):
    @classmethod
    def get_config_schema(
        cls,
        partial_validation: bool,
    ) -> typing.Dict[typing.Any, typing.Any]:
        return {
            voluptuous.Required(
                "name", default="" if partial_validation else voluptuous.UNDEFINED
            ): str,
            voluptuous.Required("method", default="merge"): voluptuous.Any(
                "rebase", "merge", "squash"
            ),
            voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
                "merge", "squash", "none", None
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

    async def _subscription_status(
        self, ctxt: context.Context
    ) -> typing.Optional[check_api.Result]:
        if self.queue_count > 1 and not ctxt.subscription.has_feature(
            subscription.Features.QUEUE_ACTION
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue with more than 1 rule set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        elif self.queue_rule.config[
            "speculative_checks"
        ] > 1 and not ctxt.subscription.has_feature(subscription.Features.QUEUE_ACTION):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue with `speculative_checks` set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        elif self.config[
            "priority"
        ] != merge_base.PriorityAliases.medium.value and not ctxt.subscription.has_feature(
            subscription.Features.PRIORITY_QUEUES
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue with `priority` set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        bot_account_result = await action_utils.validate_bot_account(
            ctxt,
            self.config["merge_bot_account"],
            option_name="merge_bot_account",
            required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
            missing_feature_message="Queue with `merge_bot_account` set is unavailable",
            # NOTE(sileht): we don't allow admin, because if branch protection are
            # enabled, but not enforced on admins, we may bypass them
            required_permissions=["write", "maintain"],
        )
        if bot_account_result is not None:
            return bot_account_result

        return None

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        subscription_status = await self._subscription_status(ctxt)
        if subscription_status:
            return subscription_status

        q = await merge_train.Train.from_context(ctxt)
        car = q.get_car(ctxt)
        if car and car.state == "updated":
            # NOTE(sileht): This car doesn't have tmp pull, so we have the
            # MERGE_QUEUE_SUMMARY and train reset here
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt, ctxt.pull_request
            )
            await delayed_refresh.plan_next_refresh(
                ctxt, [queue_rule_evaluated], ctxt.pull_request
            )

            need_reset = ctxt.has_been_synchronized() or await ctxt.is_behind
            if need_reset:
                status = check_api.Conclusion.PENDING
                ctxt.log.info("train will be reset")
                await q.reset()
            else:
                status = await merge_base.get_rule_checks_status(
                    ctxt,
                    ctxt.pull_request,
                    queue_rule_evaluated,
                    unmatched_conditions_return_failure=False,
                )
            await car.update_summaries(
                status, status, queue_rule_evaluated, will_be_reset=need_reset
            )
        elif car and car.state == "created":
            if not ctxt.has_been_only_refreshed():
                # NOTE(sileht): It's not only refreshed, so we need to
                # update the associated transient pull request.
                # This is mandatory to filter out refresh to avoid loop
                # of refreshes between this PR and the transient one.
                with utils.aredis_for_stream() as redis_stream:
                    await utils.send_refresh(
                        ctxt.repository.installation.redis,
                        redis_stream,
                        ctxt.pull["base"]["repo"],
                        pull_request_number=car.queue_pull_request_number,
                        action="internal",
                    )

        if ctxt.user_refresh_requested() or ctxt.admin_refresh_requested():
            # NOTE(sileht): user ask a refresh, we just remove the previous state of this
            # check and the method _should_be_queued will become true again :)
            check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
            if check and check_api.Conclusion(check["conclusion"]) not in [
                check_api.Conclusion.SUCCESS,
                check_api.Conclusion.PENDING,
            ]:
                await check_api.set_check_run(
                    ctxt,
                    constants.MERGE_QUEUE_SUMMARY_NAME,
                    check_api.Result(
                        check_api.Conclusion.PENDING,
                        "The pull request has been refreshed and is going to be re-embarked soon",
                        "",
                    ),
                )

        return await super().run(ctxt, rule)

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        if not ctxt.has_been_only_refreshed():
            # NOTE(sileht): It's not only refreshed, so we need to
            # update the associated transient pull request.
            # This is mandatory to filter out refresh to avoid loop
            # of refreshes between this PR and the transient one.
            q = await merge_train.Train.from_context(ctxt)
            car = q.get_car(ctxt)
            if car and car.state == "created":
                with utils.aredis_for_stream() as redis_stream:
                    await utils.send_refresh(
                        ctxt.repository.installation.redis,
                        redis_stream,
                        ctxt.pull["base"]["repo"],
                        pull_request_number=car.queue_pull_request_number,
                        action="internal",
                    )

        return await super().cancel(ctxt, rule)

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        self.config["update_bot_account"] = None
        self.config["strict"] = merge_base.StrictMergeParameter.ordered

        try:
            self.queue_rule = mergify_config["queue_rules"][self.config["name"]]
        except KeyError:
            raise voluptuous.error.Invalid(f"{self.config['name']} queue not found")

        self.queue_count = len(mergify_config["queue_rules"])
        self.config["queue_config"] = self.queue_rule.config

    async def _get_merge_queue_check(
        self, ctxt: context.Context
    ) -> typing.Optional[github_types.GitHubCheckRun]:
        return first(
            await ctxt.pull_engine_check_runs,
            key=lambda c: c["name"] == constants.MERGE_QUEUE_SUMMARY_NAME,
        )

    async def _should_be_queued(self, ctxt: context.Context, q: queue.QueueT) -> bool:
        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        return not check or check_api.Conclusion(check["conclusion"]) in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.PENDING,
        ]

    async def _should_be_merged(self, ctxt: context.Context, q: queue.QueueT) -> bool:
        if not await q.is_first_pull(ctxt):
            return False

        if not await ctxt.is_behind:
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt, ctxt.pull_request
            )
            if queue_rule_evaluated.conditions.match:
                return True

        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        if check:
            return (
                check_api.Conclusion(check["conclusion"])
                == check_api.Conclusion.SUCCESS
            )
        return False

    async def _should_be_merged_during_cancel(
        self, ctxt: context.Context, q: queue.QueueBase
    ) -> bool:
        # NOTE(sileht):
        # * The pull request have been queued (pull_request_rule has match once)
        # * we rebased the head pull request
        # * _should_be_cancel didn't remove the pull from the queue because
        #   nothing external of queue_rule unmatch
        # * queue rule conditions all match
        # * We are good to not wait user CIs list pull_request_rule but only in
        #   queue_rule
        car = typing.cast(merge_train.Train, q).get_car(ctxt)
        if car and car.state == "updated":
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt, ctxt.pull_request
            )
            # FIXME(sileht): I don't get why this is typing.Any ...
            return queue_rule_evaluated.conditions.match  # type: ignore[no-any-return]

        return False

    async def _should_be_synced(self, ctxt: context.Context, q: queue.QueueT) -> bool:
        # NOTE(sileht): done by the train itself
        return False

    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        # It's closed, it's not going to change
        if ctxt.closed:
            return True

        if ctxt.has_been_synchronized():
            return True

        q = await merge_train.Train.from_context(ctxt)
        car = q.get_car(ctxt)
        if car and car.state == "updated":
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt, ctxt.pull_request
            )
            await delayed_refresh.plan_next_refresh(
                ctxt, [queue_rule_evaluated], ctxt.pull_request
            )

            # NOTE(sileht) check first if PR should be removed from the queue
            pull_rule_checks_status = await merge_base.get_rule_checks_status(
                ctxt, ctxt.pull_request, rule
            )
            if pull_rule_checks_status == check_api.Conclusion.FAILURE:
                return True

            # NOTE(sileht): This car have been updated/rebased, so we should not cancel
            # the merge until we have a check that doesn't pass
            queue_rule_checks_status = await merge_base.get_rule_checks_status(
                ctxt,
                ctxt.pull_request,
                queue_rule_evaluated,
                unmatched_conditions_return_failure=False,
            )
            return queue_rule_checks_status == check_api.Conclusion.FAILURE

        return True

    async def _get_queue(self, ctxt: context.Context) -> queue.QueueBase:
        return await merge_train.Train.from_context(ctxt)

    async def _get_queue_summary(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.QueueBase
    ) -> str:
        car = typing.cast(merge_train.Train, q).get_car(ctxt)
        if car is None:
            return ""

        evaluated_pull = await car.get_pull_request_to_evaluate()
        queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
            ctxt, evaluated_pull
        )
        return await car.generate_merge_queue_summary(queue_rule_evaluated)

    async def send_signal(self, ctxt: context.Context) -> None:
        await signals.send(
            ctxt,
            "action.queue",
            {
                "speculative_checks": self.config["queue_config"]["speculative_checks"]
                > 1
            },
        )
