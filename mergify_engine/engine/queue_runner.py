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
from mergify_engine import merge_train
from mergify_engine import rules
from mergify_engine import utils


async def are_checks_pending(
    ctxt: context.Context, queue_rule: rules.EvaluatedQueueRule
) -> bool:
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
        for name, state in (await ctxt.checks).items()
        for cond in missing_checks_conditions
        if await cond(utils.FakePR(cond.attribute_name, name))
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

    expected_commits = (len(car.parent_pull_request_numbers) + 1) * 2
    if ctxt.pull["commits"] != expected_commits:
        ctxt.log.info(
            "train car has an unexpected number of commits",
            commits=ctxt.pull["commits"],
            expected_commits=expected_commits,
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

    train = merge_train.Train(ctxt.repository, ctxt.pull["base"]["ref"])
    await train.load()

    car = train.get_car_by_tmp_pull(ctxt)
    if not car:
        ctxt.log.warning("train car not found for an opened merge queue pull request")
        return

    try:
        queue_rule = queue_rules[car.queue_name]
    except KeyError:
        ctxt.log.warning(
            "queue_rule not found for this train car",
            queue_rules=queue_rules,
            queue_name=car.queue_name,
        )
        return

    evaluated_queue_rule = await queue_rule.get_pull_request_rule(ctxt)

    unexpected_changes = await have_unexpected_changes(ctxt, car)
    if unexpected_changes:
        mergeable = False
    else:
        mergeable = await train.is_synced_with_the_base_branch()

    if unexpected_changes:
        status = check_api.Conclusion.FAILURE
    elif not mergeable:
        status = check_api.Conclusion.PENDING
    elif not evaluated_queue_rule.missing_conditions:
        status = check_api.Conclusion.SUCCESS
    elif await are_checks_pending(ctxt, evaluated_queue_rule):
        status = check_api.Conclusion.PENDING
    else:
        status = check_api.Conclusion.FAILURE

    ctxt.log.info(
        "train car temporary pull request evaluation",
        evaluated_queue_rule=evaluated_queue_rule,
        unexpected_changes=unexpected_changes,
        mergeable=mergeable,
        status=status,
    )

    await car.update_summaries(
        ctxt,
        status,
        queue_rule=evaluated_queue_rule,
        will_be_reset=not mergeable,
    )

    if not mergeable:
        ctxt.log.info("train will be reset")
        await train.reset()

    if unexpected_changes:
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={
                "body": "This pull request has unexpected changes. The whole train will be reset."
            },
        )
