# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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
import logging
import typing

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers


ChecksCombinedStatus = typing.Literal[
    check_api.Conclusion.FAILURE,
    check_api.Conclusion.SUCCESS,
    check_api.Conclusion.PENDING,
]


async def get_rule_checks_status(
    log: "logging.LoggerAdapter[logging.Logger]",
    repository: context.Repository,
    pulls: typing.List[context.BasePullRequest],
    rule: typing.Union["rules.EvaluatedRule", "rules.EvaluatedQueueRule"],
    *,
    unmatched_conditions_return_failure: bool = True,
    use_new_rule_checks_status: bool = True,
) -> ChecksCombinedStatus:
    if rule.conditions.match:
        return check_api.Conclusion.SUCCESS

    conditions_without_checks = rule.conditions.copy()
    for condition_without_check in conditions_without_checks.walk():
        attr = condition_without_check.get_attribute_name()
        if attr.startswith("check-") or attr.startswith("status-"):
            condition_without_check.update("number>0")

    # NOTE(sileht): Something unrelated to checks unmatch?
    await conditions_without_checks(pulls)
    log.debug(
        "something unrelated to checks doesn't match? %s",
        conditions_without_checks.get_summary(),
    )
    if not conditions_without_checks.match:
        if unmatched_conditions_return_failure:
            return check_api.Conclusion.FAILURE
        else:
            return check_api.Conclusion.PENDING

    # NOTE(sileht): we replace BinaryFilter by IncompleteChecksFilter to ensure
    # all required CIs have finished. IncompleteChecksFilter return 3 states
    # instead of just True/False, this allows us to known if a condition can
    # change in the future or if its a final state.
    tree = rule.conditions.extract_raw_filter_tree()
    results: typing.Dict[int, filter.IncompleteChecksResult] = {}

    for pull in pulls:
        f = filter.IncompleteChecksFilter(
            tree,
            pending_checks=await getattr(pull, "check-pending"),
            all_checks=await pull.check,  # type: ignore[attr-defined]
        )
        live_resolvers.configure_filter(repository, f)

        ret = await f(pull)
        if ret is filter.IncompleteCheck:
            log.debug("found an incomplete check")
            return check_api.Conclusion.PENDING

        pr_number = await pull.number  # type: ignore[attr-defined]
        results[pr_number] = ret

    if all(results.values()):
        # This can't occur!, we should have returned SUCCESS earlier.
        log.error(
            "filter.IncompleteChecksFilter unexpectly returned true",
            tree=tree,
            results=results,
        )
        # So don't merge broken stuff
        return check_api.Conclusion.PENDING
    else:
        return check_api.Conclusion.FAILURE
