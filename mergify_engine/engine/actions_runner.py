# -*- encoding: utf-8 -*-
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
import base64
import typing

from datadog import statsd
import ddtrace
import yaml

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import delayed_refresh
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine.queue import merge_train
from mergify_engine.queue import naive


NOT_APPLICABLE_TEMPLATE = """<details>
<summary>Rules not applicable to this pull request:</summary>
%s
</details>"""


async def get_already_merged_summary(
    ctxt: context.Context, match: rules.RulesEvaluator
) -> str:
    if not (
        ctxt.pull["merged"]
        and any(
            (
                s["event_type"] == "pull_request"
                and typing.cast(github_types.GitHubEventPullRequest, s["data"])[
                    "action"
                ]
                == "closed"
                for s in ctxt.sources
            )
        )
    ):
        return ""

    action_merge_found_and_ran = False

    for rule in match.matching_rules:
        if "merge" in rule.actions:
            # NOTE(sileht): Replace all -merged -closed by closed/merged and
            # check it the rule still match if not it has been merged manually
            custom_conditions = rule.conditions.copy()
            for condition in custom_conditions.walk():
                attr = condition.get_attribute_name()
                if attr == "merged":
                    condition.update("merged")
                elif attr == "closed":
                    condition.update("closed")

            await custom_conditions([ctxt.pull_request])
            if custom_conditions.match:
                action_merge_found_and_ran = True

    # We already have a fully detailled status in the rule associated with the
    # action merge
    if not action_merge_found_and_ran:
        return ""

    # NOTE(sileht): This looks impossible because the pull request hasn't been
    # merged by our engine. If this pull request was a slice of another one,
    # GitHub closes it automatically and put as merged_by the merger of the
    # other one.
    if ctxt.pull["merged_by"] is None:
        merged_by = "???"
    else:
        merged_by = ctxt.pull["merged_by"]["login"]
    if merged_by == config.BOT_USER_LOGIN:
        return (
            "⚠️ The pull request has been closed by GitHub "
            "because its commits are also part of another pull request\n\n"
        )
    else:
        return "⚠️ The pull request has been merged by " f"@{merged_by}\n\n"


async def gen_summary_rules(
    ctxt: context.Context,
    _rules: typing.List[rules.EvaluatedRule],
) -> str:
    summary = ""
    for rule in _rules:
        if rule.hidden:
            continue
        if rule.disabled is None:
            summary += f"### Rule: {rule.name} ({', '.join(rule.actions)})\n"
        else:
            summary += f"### Rule: ~~{rule.name} ({', '.join(rule.actions)})~~\n"
            summary += f":no_entry_sign: **Disabled: {rule.disabled['reason']}**\n"
        summary += rule.conditions.get_summary()
        summary += "\n"
    return summary


async def gen_summary(
    ctxt: context.Context,
    pull_request_rules: rules.PullRequestRules,
    match: rules.RulesEvaluator,
) -> typing.Tuple[str, str]:

    summary = ""
    summary += await get_already_merged_summary(ctxt, match)
    if ctxt.configuration_changed:
        summary += "⚠️ The configuration has been changed, *queue* and *merge* actions are ignored. ⚠️\n\n"
    summary += await gen_summary_rules(ctxt, match.faulty_rules)
    summary += await gen_summary_rules(ctxt, match.matching_rules)
    ignored_rules = len(list(filter(lambda x: not x.hidden, match.ignored_rules)))

    if ctxt.subscription.has_feature(subscription.Features.SHOW_SPONSOR):
        summary += constants.MERGIFY_OPENSOURCE_SPONSOR_DOC

    summary += "<hr />\n"

    if ignored_rules > 0:
        summary += "<details>\n"
        if ignored_rules == 1:
            summary += f"<summary>{ignored_rules} not applicable rule</summary>\n\n"
        else:
            summary += f"<summary>{ignored_rules} not applicable rules</summary>\n\n"
        summary += await gen_summary_rules(ctxt, match.ignored_rules)
        summary += "</details>\n"

    completed_rules = len(
        list(filter(lambda rule: rule.conditions.match, match.matching_rules))
    )
    potential_rules = len(match.matching_rules) - completed_rules
    faulty_rules = len(match.faulty_rules)

    if pull_request_rules.has_user_rules():
        summary_title = []
        if faulty_rules == 1:
            summary_title.append(f"{potential_rules} faulty rule")
        elif faulty_rules > 1:
            summary_title.append(f"{potential_rules} faulty rules")

        if completed_rules == 1:
            summary_title.append(f"{completed_rules} rule matches")
        elif completed_rules > 1:
            summary_title.append(f"{completed_rules} rules match")

        if potential_rules == 1:
            summary_title.append(f"{potential_rules} potential rule")
        elif potential_rules > 1:
            summary_title.append(f"{potential_rules} potential rules")

        if completed_rules == 0 and potential_rules == 0 and faulty_rules == 0:
            summary_title.append("no rules match, no planned actions")
    else:
        summary_title = ["no rules configured, just listening for commands"]

    title = " and ".join(summary_title)
    if ctxt.configuration_changed:
        title = f"Configuration changed. This pull request must be merged manually — {title}"

    return title, summary


async def get_summary_check_result(
    ctxt: context.Context,
    pull_request_rules: rules.PullRequestRules,
    match: rules.RulesEvaluator,
    summary_check: typing.Optional[github_types.GitHubCheckRun],
    conclusions: typing.Dict[str, check_api.Conclusion],
    previous_conclusions: typing.Dict[str, check_api.Conclusion],
) -> typing.Optional[check_api.Result]:
    summary_title, summary = await gen_summary(ctxt, pull_request_rules, match)

    summary += constants.MERGIFY_PULL_REQUEST_DOC
    summary += serialize_conclusions(conclusions)

    summary_changed = (
        not summary_check
        or summary_check["output"]["title"] != summary_title
        or summary_check["output"]["summary"] != summary
        # Even the check-run content didn't change we must report the same content to
        # update the check_suite
        or ctxt.user_refresh_requested()
        or ctxt.admin_refresh_requested()
    )

    if summary_changed:
        ctxt.log.info(
            "summary changed",
            summary={
                "title": summary_title,
                "name": ctxt.SUMMARY_NAME,
                "summary": summary,
            },
            sources=ctxt.sources,
            conclusions=conclusions,
            previous_conclusions=previous_conclusions,
        )

        return check_api.Result(
            check_api.Conclusion.SUCCESS, title=summary_title, summary=summary
        )
    else:
        ctxt.log.info(
            "summary unchanged",
            summary={
                "title": summary_title,
                "name": ctxt.SUMMARY_NAME,
                "summary": summary,
            },
            sources=ctxt.sources,
            conclusions=conclusions,
            previous_conclusions=previous_conclusions,
        )
        # NOTE(sileht): Here we run the engine, but nothing change so we didn't
        # update GitHub. In pratice, only the started_at and the ended_at is
        # not up2date, we don't really care, as no action has ran
        return None


async def exec_action(
    method_name: typing.Literal["run", "cancel"],
    rule: rules.EvaluatedRule,
    action: str,
    ctxt: context.Context,
) -> check_api.Result:
    try:
        if method_name == "run":
            method = rule.actions[action].run
        elif method_name == "cancel":
            method = rule.actions[action].cancel
        else:
            raise RuntimeError("wrong method_name")
        return await method(ctxt, rule)
    except Exception as e:  # pragma: no cover
        # Forward those to worker
        if exceptions.should_be_ignored(e) or exceptions.need_retry(e):
            raise
        ctxt.log.error("action failed", action=action, rule=rule, exc_info=True)
        # TODO(sileht): extract sentry event id and post it, so
        # we can track it easly
        return check_api.Result(
            check_api.Conclusion.FAILURE, f"action '{action}' failed", ""
        )


def load_conclusions_line(
    ctxt: context.Context,
    summary_check: typing.Optional[github_types.GitHubCheckRun],
) -> typing.Optional[str]:
    if summary_check is not None and summary_check["output"]["summary"] is not None:
        lines = summary_check["output"]["summary"].splitlines()
        if not lines:
            ctxt.log.error("got summary without content", summary_check=summary_check)
            return None
        if lines[-1].startswith("<!-- ") and lines[-1].endswith(" -->"):
            return lines[-1]
    return None


def load_conclusions(
    ctxt: context.Context, summary_check: typing.Optional[github_types.GitHubCheckRun]
) -> typing.Dict[str, check_api.Conclusion]:
    line = load_conclusions_line(ctxt, summary_check)
    if line:
        return {
            name: check_api.Conclusion(conclusion)
            for name, conclusion in yaml.safe_load(
                base64.b64decode(line[5:-4].encode()).decode()
            ).items()
        }

    if not ctxt.has_been_opened():
        ctxt.log.warning(
            "previous conclusion not found in summary",
            summary_check=summary_check,
        )
    return {}


def serialize_conclusions(conclusions: typing.Dict[str, check_api.Conclusion]) -> str:
    return (
        "<!-- "
        + base64.b64encode(
            yaml.safe_dump(
                {name: conclusion.value for name, conclusion in conclusions.items()}
            ).encode()
        ).decode()
        + " -->"
    )


def get_previous_conclusion(
    previous_conclusions: typing.Dict[str, check_api.Conclusion],
    name: str,
    checks: typing.Dict[str, github_types.GitHubCheckRun],
) -> check_api.Conclusion:
    if name in previous_conclusions:
        return previous_conclusions[name]
    # NOTE(sileht): fallback on posted check-run in case we lose the Summary
    # somehow
    elif name in checks:
        return check_api.Conclusion(checks[name]["conclusion"])
    return check_api.Conclusion.NEUTRAL


async def run_actions(
    ctxt: context.Context,
    match: rules.RulesEvaluator,
    checks: typing.Dict[str, github_types.GitHubCheckRun],
    previous_conclusions: typing.Dict[str, check_api.Conclusion],
) -> typing.Dict[str, check_api.Conclusion]:
    """
    What action.run() and action.cancel() return should be reworked a bit. Currently the
    meaning is not really clear, it could be:
    - None - (succeed but no dedicated report is posted with check api
    - (None, "<title>", "<summary>") - (action is pending, for merge/backport/...)
    - ("success", "<title>", "<summary>")
    - ("failure", "<title>", "<summary>")
    - ("neutral", "<title>", "<summary>")
    - ("cancelled", "<title>", "<summary>")
    """

    user_refresh_requested = ctxt.user_refresh_requested()
    admin_refresh_requested = ctxt.admin_refresh_requested()
    actions_ran = set()
    conclusions = {}

    # NOTE(sileht): We put first rules with missing conditions to do cancellation first.
    # In case of a canceled merge action and another that need to be run. We want first
    # to remove the PR from the queue and then add it back with the new config and not the
    # reverse
    matching_rules = sorted(
        match.matching_rules, key=lambda rule: rule.conditions.match
    )

    method_name: typing.Literal["run", "cancel"]

    for rule in matching_rules:
        for action, action_obj in rule.actions.items():
            check_name = f"Rule: {rule.name} ({action})"

            done_by_another_action = (
                actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES in action_obj.flags
                and action in actions_ran
            )

            if (
                not rule.conditions.match
                or rule.disabled is not None
                or (
                    ctxt.configuration_changed
                    and actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
                    not in action_obj.flags
                )
            ):
                method_name = "cancel"
                expected_conclusions = [
                    check_api.Conclusion.NEUTRAL,
                    check_api.Conclusion.CANCELLED,
                ]
            else:
                method_name = "run"
                expected_conclusions = [
                    check_api.Conclusion.SUCCESS,
                    check_api.Conclusion.FAILURE,
                ]
                actions_ran.add(action)

            previous_conclusion = get_previous_conclusion(
                previous_conclusions, check_name, checks
            )

            need_to_be_run = (
                actions.ActionFlag.ALWAYS_RUN in action_obj.flags
                or admin_refresh_requested
                or (
                    user_refresh_requested
                    and previous_conclusion == check_api.Conclusion.FAILURE
                )
                or previous_conclusion not in expected_conclusions
            )

            # TODO(sileht): refactor it to store the whole report in the check summary,
            # not just the conclusions

            if not need_to_be_run:
                report = check_api.Result(
                    previous_conclusion, "Already in expected state", ""
                )
                message = "ignored, already in expected state"

            elif done_by_another_action:
                # NOTE(sileht) We can't run two action merge for example,
                # This assumes the action produce a report
                report = check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    f"Another {action} action already ran",
                    "",
                )
                message = "ignored, another has already been run"

            else:
                with ddtrace.tracer.trace(
                    f"action.{action}",
                    span_type="worker",
                    resource=str(ctxt),
                ) as span:
                    # NOTE(sileht): check state change so we have to run "run" or "cancel"
                    report = await exec_action(
                        method_name,
                        rule,
                        action,
                        ctxt,
                    )
                    span.set_tags({"conclusion": str(report.conclusion)})

                message = "executed"

            conclusions[check_name] = report.conclusion

            if (
                report.conclusion is not check_api.Conclusion.PENDING
                and method_name == "run"
            ):
                statsd.increment("engine.actions.count", tags=[f"name:{action}"])

            if need_to_be_run and (
                actions.ActionFlag.ALWAYS_SEND_REPORT in action_obj.flags
                or report.conclusion
                not in (
                    check_api.Conclusion.SUCCESS,
                    check_api.Conclusion.CANCELLED,
                    check_api.Conclusion.PENDING,
                )
            ):
                external_id = (
                    check_api.USER_CREATED_CHECKS
                    if actions.ActionFlag.ALLOW_RETRIGGER_MERGIFY in action_obj.flags
                    else None
                )
                try:
                    await check_api.set_check_run(
                        ctxt,
                        check_name,
                        report,
                        external_id=external_id,
                    )
                except Exception as e:
                    if exceptions.should_be_ignored(e):
                        ctxt.log.info(
                            "Fail to post check `%s`", check_name, exc_info=True
                        )
                    elif exceptions.need_retry(e):
                        raise
                    else:
                        ctxt.log.error(
                            "Fail to post check `%s`", check_name, exc_info=True
                        )

            ctxt.log.info(
                "action evaluation: `%s` %s: %s/%s -> %s",
                action,
                message,
                method_name,
                previous_conclusion.value,
                conclusions[check_name].value,
                report=report,
                previous_conclusion=previous_conclusion.value,
                conclusion=conclusions[check_name].value,
                action=action,
                check_name=check_name,
                event_types=[se["event_type"] for se in ctxt.sources],
            )

    return conclusions


async def cleanup_pending_actions_with_no_associated_rules(
    ctxt: context.Context,
    match: rules.RulesEvaluator,
    checks: typing.Dict[str, github_types.GitHubCheckRun],
    previous_conclusions: typing.Dict[str, check_api.Conclusion],
) -> None:
    check_names = {
        f"Rule: {rule.name} ({action})"
        for rule in match.matching_rules
        for action, action_obj in rule.actions.items()
    }
    removed_checks_to_cleanup = {
        check_name
        for check_name in previous_conclusions.keys()
        if check_name not in check_names
        and check_name.startswith("Rule: ")
        and (check_name.endswith(" (queue)") or check_name.endswith(" (merge)"))
        and check_name in checks
        and checks[check_name]["conclusion"] is None
    }

    for check_name in removed_checks_to_cleanup:
        ctxt.log.info("action removal cleanup", check_name=check_name)
        if check_name in checks:
            await check_api.set_check_run(
                ctxt,
                check_name,
                check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    "The rule/action does not exists anymore",
                    "",
                ),
            )

        if check_name.endswith(" (merge)"):
            await naive.Queue.force_remove_pull(ctxt)
        elif check_name.endswith(" (queue)"):
            await merge_train.Train.force_remove_pull(ctxt)


async def handle(
    pull_request_rules: rules.PullRequestRules, ctxt: context.Context
) -> typing.Optional[check_api.Result]:
    match = await pull_request_rules.get_pull_request_rule(ctxt)
    await delayed_refresh.plan_next_refresh(
        ctxt, match.matching_rules, ctxt.pull_request
    )

    if not ctxt.sources:
        # NOTE(sileht): Only comment/command, don't need to go further
        return None

    checks = {c["name"]: c for c in await ctxt.pull_engine_check_runs}

    summary_check = checks.get(ctxt.SUMMARY_NAME)
    previous_conclusions = load_conclusions(ctxt, summary_check)
    await cleanup_pending_actions_with_no_associated_rules(
        ctxt, match, checks, previous_conclusions
    )
    conclusions = await run_actions(ctxt, match, checks, previous_conclusions)
    return await get_summary_check_result(
        ctxt,
        pull_request_rules,
        match,
        summary_check,
        conclusions,
        previous_conclusions,
    )
