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

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import delayed_refresh
from mergify_engine import queue
from mergify_engine import signals
from mergify_engine import utils
from mergify_engine.actions import merge_base
from mergify_engine.actions import utils as action_utils
from mergify_engine.dashboard import subscription
from mergify_engine.queue import merge_train
from mergify_engine.rules import conditions
from mergify_engine.rules import types


if typing.TYPE_CHECKING:
    from mergify_engine import rules


class QueueAction(merge_base.MergeBaseAction):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
        # FIXME(sileht): MRGFY-562
        # | actions.ActionFlag.ALWAYS_RUN
    )

    UNQUEUE_DOCUMENTATION = f"""
You can take a look at `{constants.MERGE_QUEUE_SUMMARY_NAME}` check runs for more details.

In case of a failure due to a flaky test, you should first retrigger the CI.
Then, re-embark the pull request into the merge queue by posting the comment
`@mergifyio refresh` on the pull request.
"""

    @classmethod
    def get_config_schema(
        cls,
        partial_validation: bool,
    ) -> typing.Dict[typing.Any, typing.Any]:

        validator_default = {
            voluptuous.Required(
                "name", default="" if partial_validation else voluptuous.UNDEFINED
            ): str,
            voluptuous.Required("method", default="merge"): voluptuous.Any(
                "rebase", "merge", "squash"
            ),
            voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
                "merge", "squash", "none", None
            ),
            voluptuous.Required(
                "merge_bot_account", default=None
            ): types.Jinja2WithNone,
            voluptuous.Required(
                "update_bot_account", default=None
            ): types.Jinja2WithNone,
            voluptuous.Required("update_method", default="merge"): voluptuous.Any(
                "rebase", "merge"
            ),
            voluptuous.Required(
                "commit_message_template", default=None
            ): types.Jinja2WithNone,
            voluptuous.Required(
                "priority", default=queue.PriorityAliases.medium.value
            ): queue.PrioritySchema,
            voluptuous.Required("require_branch_protection", default=True): bool,
        }

        validator_commit_message = {
            voluptuous.Required("commit_message", default="default"): voluptuous.Any(
                "default", "title+body"
            ),
        }

        schema = validator_default
        if config.ALLOW_COMMIT_MESSAGE_OPTION:
            schema.update(validator_commit_message)
        return schema

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

        elif self.queue_rule.config[
            "batch_size"
        ] > 1 and not ctxt.subscription.has_feature(subscription.Features.QUEUE_ACTION):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue with `batch_size` set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )
        elif self.config[
            "priority"
        ] != queue.PriorityAliases.medium.value and not ctxt.subscription.has_feature(
            subscription.Features.PRIORITY_QUEUES
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Queue with `priority` set is unavailable.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        return None

    async def _update_merge_queue_summary(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: merge_train.Train,
        car: typing.Optional[merge_train.TrainCar],
    ) -> None:

        if car and car.creation_state == "updated" and not ctxt.closed:
            # NOTE(sileht): This car doesn't have tmp pull, so we have the
            # MERGE_QUEUE_SUMMARY and train reset here
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt.repository,
                ctxt.pull["base"]["ref"],
                [ctxt.pull_request],
                ctxt.log,
                ctxt.has_been_refreshed_by_timer(),
            )
            await delayed_refresh.plan_next_refresh(
                ctxt, [queue_rule_evaluated], ctxt.pull_request
            )

            unexpected_changes: typing.Optional[merge_train.UnexpectedChange]
            if await ctxt.has_been_synchronized_by_user() or await ctxt.is_behind:
                unexpected_changes = merge_train.UnexpectedUpdatedPullRequestChange(
                    ctxt.pull["number"]
                )
                status = check_api.Conclusion.PENDING
                ctxt.log.info(
                    "train will be reset", unexpected_changes=unexpected_changes
                )
                await q.reset(unexpected_changes)
            else:
                unexpected_changes = None
                status = await merge_base.get_rule_checks_status(
                    ctxt.log,
                    ctxt.repository,
                    [ctxt.pull_request],
                    queue_rule_evaluated,
                    unmatched_conditions_return_failure=False,
                )
            await car.update_state(status, queue_rule_evaluated)
            await car.update_summaries(status, unexpected_change=unexpected_changes)
            await q.save()

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        subscription_status = await self._subscription_status(ctxt)
        if subscription_status:
            return subscription_status

        # FIXME(sileht): we should use the computed update_bot_account in TrainCar.update_pull(),
        # not the original one
        try:
            await action_utils.render_bot_account(
                ctxt,
                self.config["update_bot_account"],
                option_name="update_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Queue with `update_bot_account` set is unavailable",
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        try:
            merge_bot_account = await action_utils.render_bot_account(
                ctxt,
                self.config["merge_bot_account"],
                option_name="merge_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Queue with `merge_bot_account` set is unavailable",
                # NOTE(sileht): we don't allow admin, because if branch protection are
                # enabled, but not enforced on admins, we may bypass them
                required_permissions=["write", "maintain"],
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        q = await merge_train.Train.from_context(ctxt)
        car = q.get_car(ctxt)
        await self._update_merge_queue_summary(ctxt, rule, q, car)

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

        self._set_effective_priority(ctxt)

        result = await self.merge_report(ctxt)
        if result is None:
            if await self._should_be_queued(ctxt, q):
                await q.add_pull(ctxt, typing.cast(queue.PullQueueConfig, self.config))
                try:
                    if await self._should_be_merged(ctxt, rule, q):
                        result = await self._merge(ctxt, rule, q, merge_bot_account)
                    else:
                        result = await self.get_queue_status(ctxt, rule, q)
                except Exception:
                    await q.remove_pull(ctxt)
                    raise
            else:
                await q.remove_pull(ctxt)
                result = check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    "The pull request has been removed from the queue",
                    "The queue conditions cannot be satisfied due to failing checks or checks timeout. "
                    f"{self.UNQUEUE_DOCUMENTATION}",
                )

        if result.conclusion is not check_api.Conclusion.PENDING:
            await q.remove_pull(ctxt)

        # NOTE(sileht): Only refresh if the car still exists and is the same as
        # before we run the action
        new_car = q.get_car(ctxt)
        if (
            car
            and car.queue_pull_request_number is not None
            and new_car
            and new_car.creation_state == "created"
            and new_car.queue_pull_request_number is not None
            and new_car.queue_pull_request_number == car.queue_pull_request_number
            and self.need_draft_pull_request_refresh()
            and not ctxt.has_been_only_refreshed()
        ):
            # NOTE(sileht): It's not only refreshed, so we need to
            # update the associated transient pull request.
            # This is mandatory to filter out refresh to avoid loop
            # of refreshes between this PR and the transient one.
            with utils.yaaredis_for_stream() as redis_stream:
                await utils.send_pull_refresh(
                    ctxt.repository.installation.redis,
                    redis_stream,
                    ctxt.pull["base"]["repo"],
                    pull_request_number=new_car.queue_pull_request_number,
                    action="internal",
                    source="forward from queue action (run)",
                )
        return result

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        # FIXME(sileht): we should use the computed update_bot_account in TrainCar.update_pull(),
        # not the original one
        try:
            await action_utils.render_bot_account(
                ctxt,
                self.config["update_bot_account"],
                option_name="update_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Queue with `update_bot_account` set is unavailable",
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        self._set_effective_priority(ctxt)

        q = await merge_train.Train.from_context(ctxt)
        car = q.get_car(ctxt)
        await self._update_merge_queue_summary(ctxt, rule, q, car)

        result = await self.merge_report(ctxt)
        if result is None:
            # We just rebase the pull request, don't cancel it yet if CIs are
            # running. The pull request will be merged if all rules match again.
            # if not we will delete it when we received all CIs termination
            if await self._should_be_cancel(ctxt, rule, q):
                result = actions.CANCELLED_CHECK_REPORT
            else:
                result = await self.get_queue_status(ctxt, rule, q)
        elif not ctxt.closed:
            result = actions.CANCELLED_CHECK_REPORT

        if result.conclusion is not check_api.Conclusion.PENDING:
            await q.remove_pull(ctxt)

        # The car may have been removed
        newcar = q.get_car(ctxt)
        # NOTE(sileht): Only refresh if the car still exists
        if (
            newcar
            and newcar.creation_state == "created"
            and newcar.queue_pull_request_number is not None
            and self.need_draft_pull_request_refresh()
            and not ctxt.has_been_only_refreshed()
        ):
            # NOTE(sileht): It's not only refreshed, so we need to
            # update the associated transient pull request.
            # This is mandatory to filter out refresh to avoid loop
            # of refreshes between this PR and the transient one.
            with utils.yaaredis_for_stream() as redis_stream:
                await utils.send_pull_refresh(
                    ctxt.repository.installation.redis,
                    redis_stream,
                    ctxt.pull["base"]["repo"],
                    pull_request_number=newcar.queue_pull_request_number,
                    action="internal",
                    source="forward from queue action (cancel)",
                )
        return result

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        if not config.ALLOW_COMMIT_MESSAGE_OPTION:
            self.config["commit_message"] = "default"

        try:
            self.queue_rule = mergify_config["queue_rules"][self.config["name"]]
        except KeyError:
            raise voluptuous.error.Invalid(f"{self.config['name']} queue not found")

        self.queue_count = len(mergify_config["queue_rules"])
        self.config["queue_config"] = self.queue_rule.config

    def _set_effective_priority(self, ctxt: context.Context) -> None:
        self.config["effective_priority"] = typing.cast(
            int,
            self.config["priority"]
            + self.config["queue_config"]["priority"] * queue.QUEUE_PRIORITY_OFFSET,
        )

    async def _should_be_queued(self, ctxt: context.Context, q: queue.QueueT) -> bool:
        # automatically re-embark pull request if rules matches again in some cases
        if (
            self.config["queue_config"]["speculative_checks"] == 1
            and self.config["queue_config"]["batch_size"] == 1
            and self.config["queue_config"]["allow_inplace_checks"]
        ):
            return True

        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        return not check or check_api.Conclusion(check["conclusion"]) in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.PENDING,
        ]

    async def _should_be_merged(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.QueueT
    ) -> bool:
        if not await q.is_first_pull(ctxt):
            return False

        car = typing.cast(merge_train.Train, q).get_car(ctxt)
        if car is None:
            return False

        return car.checks_conclusion == check_api.Conclusion.SUCCESS

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

        car = typing.cast(merge_train.Train, q).get_car(ctxt)
        if car and car.creation_state == "updated":
            queue_rule_evaluated = await self.queue_rule.get_pull_request_rule(
                ctxt.repository,
                ctxt.pull["base"]["ref"],
                [ctxt.pull_request],
                ctxt.log,
                ctxt.has_been_refreshed_by_timer(),
            )

            # NOTE(sileht) check first if PR should be removed from the queue
            pull_rule_checks_status = await merge_base.get_rule_checks_status(
                ctxt.log, ctxt.repository, [ctxt.pull_request], rule
            )
            if pull_rule_checks_status == check_api.Conclusion.FAILURE:
                return True

            # NOTE(sileht): if the pull request rules are pending we wait their
            # match before checking queue rules states, in case of one
            # condition suddently unqueue the pull request.
            # TODO(sileht): we may want to make this behavior configurable as
            # people having slow/long CI running only on pull request rules, we
            # may want to merge it before it finishes.
            elif pull_rule_checks_status == check_api.Conclusion.PENDING:
                return False

            # NOTE(sileht): This car have been updated/rebased, so we should not cancel
            # the merge until we have a check that doesn't pass
            queue_rule_checks_status = await merge_base.get_rule_checks_status(
                ctxt.log,
                ctxt.repository,
                [ctxt.pull_request],
                queue_rule_evaluated,
                unmatched_conditions_return_failure=False,
            )
            return queue_rule_checks_status == check_api.Conclusion.FAILURE

        return True

    async def get_queue_status(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: typing.Optional[queue.QueueBase],
    ) -> check_api.Result:
        if q is None:
            title = "The pull request will be merged soon"
        else:
            position = await q.get_position(ctxt)
            if position is None:
                ctxt.log.error("expected queued pull request not found in queue")
                title = "The pull request is queued to be merged"
            else:
                _ord = utils.to_ordinal_numeric(position + 1)
                title = f"The pull request is the {_ord} in the queue to be merged"

        summary = await typing.cast(merge_train.Train, q).get_pull_summary(
            ctxt, self.queue_rule
        )
        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    async def send_signal(self, ctxt: context.Context) -> None:
        await signals.send(
            ctxt,
            "action.queue",
            {
                "speculative_checks": self.config["queue_config"]["speculative_checks"]
                > 1,
                "batch_size": self.config["queue_config"]["batch_size"] > 1,
                "allow_inplace_checks": self.config["queue_config"][
                    "allow_inplace_checks"
                ],
                "commit_message": self.config["commit_message"],
                "commit_message_set": "commit_message" in self.raw_config,
            },
        )

    def need_draft_pull_request_refresh(self) -> bool:
        # NOTE(sileht): if the queue rules don't use attributes linked to the
        # original pull request we don't need to refresh the draft pull request
        conditions = self.queue_rule.conditions.copy()
        for cond in conditions.walk():
            attr = cond.get_attribute_name()
            if attr not in context.QueuePullRequest.QUEUE_ATTRIBUTES:
                return True
        return False

    async def get_conditions_requirements(
        self,
        ctxt: context.Context,
    ) -> typing.List[
        typing.Union[conditions.RuleConditionGroup, conditions.RuleCondition]
    ]:
        branch_protection_conditions = []
        if (
            self.config["require_branch_protection"]
            or self.queue_rule.config["speculative_checks"] > 1
        ):
            branch_protection_conditions = (
                await conditions.get_branch_protection_conditions(
                    ctxt.repository, ctxt.pull["base"]["ref"]
                )
            )
        depends_on_conditions = await conditions.get_depends_on_conditions(ctxt)
        return branch_protection_conditions + depends_on_conditions
