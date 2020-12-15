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


from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import merge_train
from mergify_engine import rules
from mergify_engine import utils


def are_checks_pending(
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
        for name, state in ctxt.checks.items()
        for cond in missing_checks_conditions
        if cond(utils.FakePR(cond.attribute_name, name))
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


def handle(queue_rules: rules.QueueRules, ctxt: context.Context) -> None:
    # FIXME: Maybe create a command to force the retesting to put back the PR in the queue?

    ctxt.log.info("handling train car temporary pull request event")

    if ctxt.pull["state"] == "closed":
        ctxt.log.info("train car temporary pull request has been closed")
        return

    train = merge_train.Train(
        ctxt.client,
        utils.get_redis_for_cache(),
        ctxt.pull["base"]["repo"]["owner"]["id"],
        ctxt.pull["base"]["repo"]["owner"]["login"],
        ctxt.pull["base"]["repo"]["id"],
        ctxt.pull["base"]["repo"]["name"],
        ctxt.pull["base"]["ref"],
    )

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

    evaluated_queue_rule = queue_rule.get_pull_request_rule(ctxt)
    mergeable = train.is_synced_with_the_base_branch()

    if not mergeable:
        status = check_api.Conclusion.PENDING
    elif not evaluated_queue_rule.missing_conditions:
        status = check_api.Conclusion.SUCCESS
    elif are_checks_pending(ctxt, evaluated_queue_rule):
        status = check_api.Conclusion.PENDING
    else:
        status = check_api.Conclusion.FAILURE

    ctxt.log.info(
        "train car temporary pull request evaluation",
        evaluated_queue_rule=evaluated_queue_rule,
        mergeable=mergeable,
        status=status,
    )

    car.update_summaries(ctxt, evaluated_queue_rule, status, deleted=not mergeable)

    if not mergeable:
        ctxt.log.info("train will be reset")
        train.reset()
