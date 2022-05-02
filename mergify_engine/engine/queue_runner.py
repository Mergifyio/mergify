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

from first import first

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import delayed_refresh
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.queue import merge_train
from mergify_engine.rules import checks_status


async def have_unexpected_draft_pull_request_changes(
    ctxt: context.Context, car: merge_train.TrainCar
) -> bool:
    unexpected_event = first(
        (source for source in ctxt.sources),
        key=lambda s: s["event_type"] == "pull_request"
        and typing.cast(github_types.GitHubEventPullRequest, s["data"])["action"]
        in ["closed", "reopened", "synchronize"],
    )
    if unexpected_event:
        ctxt.log.info(
            "train car received an unexpected event",
            unexpected_event=unexpected_event,
        )
        return True

    return False


async def handle(queue_rules: rules.QueueRules, ctxt: context.Context) -> None:
    # FIXME: Maybe create a command to force the retesting to put back the PR in the queue?

    train = await merge_train.Train.from_context(ctxt)

    car = train.get_car_by_tmp_pull(ctxt)
    if not car:
        if ctxt.closed:
            ctxt.log.info(
                "train car temporary pull request has been closed", sources=ctxt.sources
            )
        else:
            ctxt.log.warning(
                "train car not found for an opened merge queue pull request",
                sources=ctxt.sources,
            )

        return

    if car.checks_conclusion != check_api.Conclusion.PENDING and ctxt.closed:
        ctxt.log.info(
            "train car temporary pull request has been closed", sources=ctxt.sources
        )
        return

    if car.queue_pull_request_number is None:
        raise RuntimeError(
            "Got draft pull request event on car without queue_pull_request_number"
        )

    ctxt.log.info(
        "handling train car temporary pull request event",
        sources=ctxt.sources,
        gh_pulls_queued=[
            ep.user_pull_request_number for ep in car.still_queued_embarked_pulls
        ],
    )

    queue_name = car.still_queued_embarked_pulls[0].config["name"]
    try:
        queue_rule = queue_rules[queue_name]
    except KeyError:
        ctxt.log.warning(
            "queue_rule not found for this train car",
            gh_pulls_queued=[
                ep.user_pull_request_number for ep in car.still_queued_embarked_pulls
            ],
            queue_rules=queue_rules,
            queue_name=queue_name,
        )
        return

    pull_requests = await car.get_pull_requests_to_evaluate()
    evaluated_queue_rule = await queue_rule.get_evaluated_queue_rule(
        ctxt.repository,
        ctxt.pull["base"]["ref"],
        pull_requests,
        ctxt.log,
        ctxt.has_been_refreshed_by_timer(),
    )

    for pull_request in pull_requests:
        await delayed_refresh.plan_next_refresh(
            ctxt, [evaluated_queue_rule], pull_request
        )

    if not ctxt.sources:
        # NOTE(sileht): Only comment/command, don't need to go further
        return None

    unexpected_changes: typing.Optional[merge_train.UnexpectedChange] = None
    if await have_unexpected_draft_pull_request_changes(ctxt, car):
        unexpected_changes = merge_train.UnexpectedDraftPullRequestChange(
            car.queue_pull_request_number
        )
    else:
        current_base_sha = await train.get_base_sha()
        if not await train.is_synced_with_the_base_branch(current_base_sha):
            unexpected_changes = merge_train.UnexpectedBaseBranchChange(
                current_base_sha
            )

    if unexpected_changes is None:
        real_status = status = await checks_status.get_rule_checks_status(
            ctxt.log,
            ctxt.repository,
            pull_requests,
            evaluated_queue_rule,
            unmatched_conditions_return_failure=False,
        )
        if real_status == check_api.Conclusion.FAILURE and (
            not car.has_previous_car_status_succeeded()
            or len(car.initial_embarked_pulls) != 1
        ):
            # NOTE(sileht): we can't set it as failed as we don't known
            # yet which pull request is responsible for the failure.
            # * one of the batch ?
            # * one of the parent car ?
            status = check_api.Conclusion.PENDING
    else:
        real_status = status = check_api.Conclusion.PENDING

    ctxt.log.info(
        "train car temporary pull request evaluation",
        gh_pull_queued=[
            ep.user_pull_request_number for ep in car.still_queued_embarked_pulls
        ],
        evaluated_queue_rule=evaluated_queue_rule.conditions.get_summary(),
        unexpected_changes=unexpected_changes,
        temporary_status=status,
        real_status=real_status,
        event_types=[se["event_type"] for se in ctxt.sources],
    )

    await car.update_state(real_status, evaluated_queue_rule)
    await car.update_summaries(status, unexpected_change=unexpected_changes)
    await train.save()

    if unexpected_changes:
        ctxt.log.info(
            "train will be reset",
            gh_pull_queued=[
                ep.user_pull_request_number for ep in car.still_queued_embarked_pulls
            ],
            unexpected_changes=unexpected_changes,
        )
        await train.reset(unexpected_changes)

        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={
                "body": f"This pull request has unexpected changes: {unexpected_changes}. The whole train will be reset."
            },
        )
