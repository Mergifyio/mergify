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
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import delayed_refresh
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import signals
from mergify_engine import utils
from mergify_engine.actions import merge_base
from mergify_engine.actions import utils as action_utils
from mergify_engine.dashboard import subscription
from mergify_engine.queue import freeze
from mergify_engine.queue import merge_train
from mergify_engine.rules import checks_status
from mergify_engine.rules import conditions
from mergify_engine.rules import types


if typing.TYPE_CHECKING:
    from mergify_engine import rules


class QueueAction(merge_base.MergeBaseAction[merge_train.Train]):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
        | actions.ActionFlag.SUCCESS_IS_FINAL_STATE
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
        return {
            voluptuous.Required(
                "name", default="" if partial_validation else voluptuous.UNDEFINED
            ): str,
            voluptuous.Required("method", default="merge"): voluptuous.Any(
                "rebase",
                "merge",
                "squash",
                "fast-forward",
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
            voluptuous.Required("update_method", default=None): voluptuous.Any(
                "rebase", "merge", None
            ),
            voluptuous.Required(
                "commit_message_template", default=None
            ): types.Jinja2WithNone,
            voluptuous.Required(
                "priority", default=queue.PriorityAliases.medium.value
            ): queue.PrioritySchema,
            voluptuous.Required("require_branch_protection", default=True): bool,
        }

    async def _subscription_status(
        self, ctxt: context.Context
    ) -> typing.Optional[check_api.Result]:
        if self.queue_count > 1 and not ctxt.subscription.has_feature(
            subscription.Features.QUEUE_ACTION
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Cannot use multiple queues.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        elif self.queue_rule.config[
            "speculative_checks"
        ] > 1 and not ctxt.subscription.has_feature(subscription.Features.QUEUE_ACTION):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Cannot use `speculative_checks` with queue action.",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        elif self.queue_rule.config[
            "batch_size"
        ] > 1 and not ctxt.subscription.has_feature(subscription.Features.QUEUE_ACTION):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Cannot use `batch_size` with queue action.",
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
                "Cannot use `priority` with queue action.",
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
            queue_rule_evaluated = await self.queue_rule.get_evaluated_queue_rule(
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
            status: checks_status.ChecksCombinedStatus
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
                status = await checks_status.get_rule_checks_status(
                    ctxt.log,
                    ctxt.repository,
                    [ctxt.pull_request],
                    queue_rule_evaluated,
                    unmatched_conditions_return_failure=False,
                )
            await car.update_state(status, queue_rule_evaluated)
            await car.update_summaries(status, unexpected_change=unexpected_changes)
            await q.save()

    async def _render_bot_account(
        self, ctxt: context.Context
    ) -> typing.Optional[github_types.GitHubLogin]:
        return await action_utils.render_bot_account(
            ctxt,
            self.config["merge_bot_account"],
            option_name="merge_bot_account",
            required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
            missing_feature_message="Cannot use `merge_bot_account` with queue action",
            # NOTE(sileht): we don't allow admin, because if branch protection are
            # enabled, but not enforced on admins, we may bypass them
            required_permissions=["write", "maintain"],
        )

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        subscription_status = await self._subscription_status(ctxt)
        if subscription_status:
            return subscription_status

        if self.config["method"] == "fast-forward":
            if self.config["update_method"] != "rebase":
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"`update_method: {self.config['update_method']}` is not compatible with fast-forward merge method",
                    "`update_method` must be set to `rebase`.",
                )
            elif self.config["commit_message_template"] is not None:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Commit message can't be changed with fast-forward merge method",
                    "`commit_message_template` must not be set if `method: fast-forward` is set.",
                )
            elif self.queue_rule.config["batch_size"] > 1:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "batch_size > 1 is not compatible with fast-forward merge method",
                    "The merge `method` or the queue configuration must be updated.",
                )
            elif self.queue_rule.config["speculative_checks"] > 1:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "speculative_checks > 1 is not compatible with fast-forward merge method",
                    "The merge `method` or the queue configuration must be updated.",
                )
            elif not self.queue_rule.config["allow_inplace_checks"]:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "allow_inplace_checks=False is not compatible with fast-forward merge method",
                    "The merge `method` or the queue configuration must be updated.",
                )

        protection = await ctxt.repository.get_branch_protection(
            ctxt.pull["base"]["ref"]
        )
        if (
            protection
            and "required_status_checks" in protection
            and "strict" in protection["required_status_checks"]
            and protection["required_status_checks"]["strict"]
        ):
            if self.queue_rule.config["batch_size"] > 1:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "batch_size > 1 is not compatible with branch protection setting",
                    "The branch protection setting `Require branches to be up to date before merging` must be unset.",
                )
            elif self.queue_rule.config["speculative_checks"] > 1:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "speculative_checks > 1 is not compatible with branch protection setting",
                    "The branch protection setting `Require branches to be up to date before merging` must be unset.",
                )

        # FIXME(sileht): we should use the computed update_bot_account in TrainCar.update_pull(),
        # not the original one
        try:
            await action_utils.render_bot_account(
                ctxt,
                self.config["update_bot_account"],
                option_name="update_bot_account",
                required_feature=subscription.Features.MERGE_BOT_ACCOUNT,
                missing_feature_message="Cannot use `update_bot_account` with queue action",
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        try:
            merge_bot_account = await self._render_bot_account(ctxt)
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
                check_api.Conclusion.NEUTRAL,
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

        result = await self.merge_report(ctxt, merge_bot_account)
        if result is None:
            if await self._should_be_queued(ctxt, q):
                await q.add_pull(
                    ctxt,
                    typing.cast(queue.PullQueueConfig, self.config),
                    rule.get_signal_trigger(),
                )
                try:
                    qf = await freeze.QueueFreeze.get(
                        ctxt.repository, self.config["name"]
                    )
                    if await self._should_be_merged(ctxt, q, qf):
                        result = await self._merge(ctxt, rule, q, merge_bot_account)
                    else:
                        result = await self.get_queue_status(ctxt, rule, q, qf)

                except Exception:
                    await q.remove_pull(ctxt, rule.get_signal_trigger())
                    raise
            else:
                result = await self.get_unqueue_status(ctxt, q)

        if result.conclusion is not check_api.Conclusion.PENDING:
            # NOTE(sileht): The PR has been checked successfully but the
            # final merge fail, we must erase the queue summary conclusion,
            # so the requeue can works.
            if (
                car
                and car.checks_conclusion == check_api.Conclusion.SUCCESS
                and result.conclusion is check_api.Conclusion.CANCELLED
            ):
                await check_api.set_check_run(
                    ctxt,
                    constants.MERGE_QUEUE_SUMMARY_NAME,
                    check_api.Result(
                        check_api.Conclusion.CANCELLED,
                        f"The pull request {ctxt.pull['number']} cannot be merged and has been disembarked",
                        result.title + "\n" + result.summary,
                    ),
                )

            await q.remove_pull(ctxt, rule.get_signal_trigger())

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
            await utils.send_pull_refresh(
                ctxt.repository.installation.redis.stream,
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
                missing_feature_message="Cannot use `update_bot_account` with queue action",
            )
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        try:
            merge_bot_account = await self._render_bot_account(ctxt)
        except action_utils.RenderBotAccountFailure as e:
            return check_api.Result(e.status, e.title, e.reason)

        self._set_effective_priority(ctxt)

        q = await merge_train.Train.from_context(ctxt)
        car = q.get_car(ctxt)
        await self._update_merge_queue_summary(ctxt, rule, q, car)

        result = await self.merge_report(ctxt, merge_bot_account)
        if result is None:
            # We just rebase the pull request, don't cancel it yet if CIs are
            # running. The pull request will be merged if all rules match again.
            # if not we will delete it when we received all CIs termination
            if await self._should_be_queued(ctxt, q):
                if await self._should_be_cancel(ctxt, rule, q):
                    result = actions.CANCELLED_CHECK_REPORT
                else:
                    result = await self.get_queue_status(ctxt, rule, q)
            else:
                result = await self.get_unqueue_status(ctxt, q)

        if result.conclusion is not check_api.Conclusion.PENDING:
            await q.remove_pull(ctxt, rule.get_signal_trigger())

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
            await utils.send_pull_refresh(
                ctxt.repository.installation.redis.stream,
                ctxt.pull["base"]["repo"],
                pull_request_number=newcar.queue_pull_request_number,
                action="internal",
                source="forward from queue action (cancel)",
            )
        return result

    def validate_config(self, mergify_config: "rules.MergifyConfig") -> None:
        if self.config["update_method"] is None:
            self.config["update_method"] = (
                "rebase" if self.config["method"] == "fast-forward" else "merge"
            )

        try:
            self.queue_rule = mergify_config["queue_rules"][self.config["name"]]
        except KeyError:
            raise voluptuous.error.Invalid(f"{self.config['name']} queue not found")

        self.queue_count = len(mergify_config["queue_rules"])

    def _set_effective_priority(self, ctxt: context.Context) -> None:
        self.config["effective_priority"] = typing.cast(
            int,
            self.config["priority"]
            + self.queue_rule.config["priority"] * queue.QUEUE_PRIORITY_OFFSET,
        )

    async def get_unqueue_status(
        self, ctxt: context.Context, q: merge_train.Train
    ) -> check_api.Result:
        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        manually_unqueued = (
            check
            and check_api.Conclusion(check["conclusion"])
            == check_api.Conclusion.CANCELLED
        )
        if manually_unqueued:
            reason = "The pull request has been manually removed from the queue by an `unqueue` command."
        else:
            reason = (
                "The queue conditions cannot be satisfied due to failing checks or checks timeout. "
                f"{self.UNQUEUE_DOCUMENTATION}"
            )
        return check_api.Result(
            check_api.Conclusion.CANCELLED,
            "The pull request has been removed from the queue",
            reason,
        )

    async def _should_be_queued(
        self, ctxt: context.Context, q: merge_train.Train
    ) -> bool:
        check = await ctxt.get_engine_check_run(constants.MERGE_QUEUE_SUMMARY_NAME)
        return not check or check_api.Conclusion(check["conclusion"]) in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.PENDING,
            check_api.Conclusion.NEUTRAL,
        ]

    async def _should_be_merged(
        self,
        ctxt: context.Context,
        q: merge_train.Train,
        qf: typing.Optional[freeze.QueueFreeze],
    ) -> bool:

        if qf is not None:
            return False

        if not await q.is_first_pull(ctxt):
            return False

        car = q.get_car(ctxt)
        if car is None:
            return False

        return car.checks_conclusion == check_api.Conclusion.SUCCESS

    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: merge_train.Train
    ) -> bool:
        # It's closed, it's not going to change
        if ctxt.closed:
            return True

        if await ctxt.has_been_synchronized_by_user():
            return True

        position, _ = q.find_embarked_pull(ctxt.pull["number"])
        if position is None:
            return True

        car = q.get_car(ctxt)
        if car and car.creation_state == "updated":
            # NOTE(sileht) check first if PR should be removed from the queue
            pull_rule_checks_status = await checks_status.get_rule_checks_status(
                ctxt.log, ctxt.repository, [ctxt.pull_request], rule
            )
            # NOTE(sileht): if the pull request rules are pending we wait their
            # match before checking queue rules states, in case of one
            # condition suddently unqueue the pull request.
            # TODO(sileht): we may want to make this behavior configurable as
            # people having slow/long CI running only on pull request rules, we
            # may want to merge it before it finishes.
            return pull_rule_checks_status not in (
                check_api.Conclusion.PENDING,
                check_api.Conclusion.SUCCESS,
            )

        return True

    async def get_queue_status(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: merge_train.Train,
        qf: typing.Optional[freeze.QueueFreeze] = None,
    ) -> check_api.Result:
        if qf is not None:
            title = f'The queue "{qf.name}" is currently frozen, for the following reason: {qf.reason}'
        else:
            position, _ = q.find_embarked_pull(ctxt.pull["number"])
            if position is None:
                ctxt.log.error("expected queued pull request not found in queue")
                title = "The pull request is queued to be merged"
            else:
                _ord = utils.to_ordinal_numeric(position + 1)
                title = f"The pull request is the {_ord} in the queue to be merged"

        summary = await q.get_pull_summary(ctxt, self.queue_rule)
        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    async def send_signal(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: merge_train.Train
    ) -> None:
        _, embarked_pull = q.find_embarked_pull(ctxt.pull["number"])
        if embarked_pull is None:
            raise RuntimeError("Queue pull request with no embarked_pull")
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.queue.merged",
            signals.EventQueueMergedMetadata(
                {
                    "queue_name": self.config["name"],
                    "branch": ctxt.pull["base"]["ref"],
                    "queued_at": embarked_pull.embarked_pull.queued_at,
                }
            ),
            rule.get_signal_trigger(),
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
                    ctxt.repository, ctxt.pull["base"]["ref"], strict=False
                )
            )
        depends_on_conditions = await conditions.get_depends_on_conditions(ctxt)
        return branch_protection_conditions + depends_on_conditions
