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
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.queue import merge_train


async def have_unexpected_changes(
    ctxt: context.Context, car: merge_train.TrainCar
) -> bool:
    if ctxt.pull["base"]["sha"] != car.initial_current_base_sha:
        ctxt.log.info(
            "train car has an unexpected base sha change",
            base_sha=ctxt.pull["base"]["sha"],
            initial_current_base_sha=car.initial_current_base_sha,
        )
        return True

    if ctxt.have_been_synchronized():
        ctxt.log.info(
            "train car has unexpectedly been synchronized",
        )
        return True

    unexpected_event = first(
        (source for source in ctxt.sources),
        key=lambda s: s["event_type"] == "pull_request"
        and typing.cast(github_types.GitHubEventPullRequest, s["data"])["action"]
        in ["closed", "reopened"],
    )
    if unexpected_event:
        ctxt.log.debug(
            "train car received an unexpected event",
            unexpected_event=unexpected_event,
        )
        return True

    return False


async def handle(queue_rules: rules.QueueRules, ctxt: context.Context) -> None:
    # FIXME: Maybe create a command to force the retesting to put back the PR in the queue?

    ctxt.log.info("handling train car temporary pull request event")

    if ctxt.pull["state"] == "closed":
        ctxt.log.info("train car temporary pull request has been closed")
        return

    train = await merge_train.Train.from_context(ctxt)

    car = train.get_car_by_tmp_pull(ctxt)
    if not car:
        ctxt.log.warning("train car not found for an opened merge queue pull request")
        return

    try:
        queue_rule = queue_rules[car.config["name"]]
    except KeyError:
        ctxt.log.warning(
            "queue_rule not found for this train car",
            queue_rules=queue_rules,
            queue_name=car.config["name"],
        )
        return

    evaluated_queue_rule = await queue_rule.get_pull_request_rule(ctxt)

    unexpected_changes = await have_unexpected_changes(ctxt, car)
    if unexpected_changes:
        need_reset = True
    else:
        need_reset = not await train.is_synced_with_the_base_branch()

    if need_reset:
        real_status = status = check_api.Conclusion.PENDING
    else:
        real_status = status = await merge_train.get_queue_rule_checks_status(
            ctxt, evaluated_queue_rule
        )
        if (
            real_status == check_api.Conclusion.FAILURE
            and not await car.has_previous_car_status_succeed()
        ):
            status = check_api.Conclusion.PENDING

    ctxt.log.info(
        "train car temporary pull request evaluation",
        evaluated_queue_rule=evaluated_queue_rule,
        unexpected_changes=unexpected_changes,
        reseted=need_reset,
        status=status,
        real_status=real_status,
        event_types=[se["event_type"] for se in ctxt.sources],
    )

    await car.update_summaries(
        status, evaluated_queue_rule=evaluated_queue_rule, will_be_reset=need_reset
    )

    if need_reset:
        ctxt.log.info("train will be reset")
        await train.reset()

    if unexpected_changes:
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={
                "body": "This pull request has unexpected changes. The whole train will be reset."
            },
        )
