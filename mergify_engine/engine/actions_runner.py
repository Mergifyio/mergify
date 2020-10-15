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
import copy

from datadog import statsd
import yaml

from mergify_engine import check_api
from mergify_engine import doc
from mergify_engine import utils


NOT_APPLICABLE_TEMPLATE = """<details>
<summary>Rules not applicable to this pull request:</summary>
%s
</details>"""


def get_already_merged_summary(ctxt, match):
    if not (
        ctxt.pull["merged"]
        and any(
            (
                s["event_type"] == "pull_request" and s["data"]["action"] == "closed"
                for s in ctxt.sources
            )
        )
    ):
        return ""

    action_merge_found = False
    action_merge_found_in_active_rule = False

    for rule, missing_conditions in match.matching_rules:
        if "merge" in rule.actions:
            action_merge_found = True
            if not missing_conditions:
                action_merge_found_in_active_rule = True

    # We already have a fully detailled status in the rule associated with the
    # action merge
    if not action_merge_found or action_merge_found_in_active_rule:
        return ""

    # NOTE(sileht): While this looks impossible because the pull request haven't been
    # merged by our engine. If this pull request was a slice of another one, Github close
    # it automatically and put as merged_by the merger of the other one.
    if ctxt.pull["merged_by"]["login"] in ["mergify[bot]", "mergify-test[bot]"]:
        return (
            "⚠️ The pull request has been closed by GitHub "
            "because its commits are also part of another pull request\n\n"
        )
    else:
        return (
            "⚠️ The pull request has been merged by "
            "@%s\n\n" % ctxt.pull["merged_by"]["login"]
        )


def gen_summary_rules(rules):
    summary = ""
    for rule, missing_conditions in rules:
        if rule.hidden:
            continue
        summary += "#### Rule: %s" % rule.name
        summary += " (%s)" % ", ".join(rule.actions)
        for cond in rule.conditions:
            checked = " " if cond in missing_conditions else "X"
            summary += "\n- [%s] `%s`" % (checked, cond)
        summary += "\n\n"
    return summary


def gen_summary(ctxt, match):
    summary = ""
    summary += get_already_merged_summary(ctxt, match)
    summary += gen_summary_rules(match.matching_rules)
    ignored_rules = len(list(filter(lambda x: not x[0].hidden, match.ignored_rules)))

    if not ctxt.subscription.active:
        summary += (
            "<hr />\n"
            ":sparkling_heart:&nbsp;&nbsp;Mergify is proud to provide this service "
            "for free to open source projects.\n\n"
            ":rocket:&nbsp;&nbsp;You can help us by [becoming a sponsor](/sponsors/Mergifyio)!\n"
        )

    summary += "<hr />\n"

    if ignored_rules > 0:
        summary += "<details>\n"
        if ignored_rules == 1:
            summary += "<summary>%d not applicable rule</summary>\n\n" % ignored_rules
        else:
            summary += "<summary>%d not applicable rules</summary>\n\n" % ignored_rules
        summary += gen_summary_rules(match.ignored_rules)
        summary += "</details>\n"

    completed_rules = len(list(filter(lambda x: not x[1], match.matching_rules)))
    potential_rules = len(match.matching_rules) - completed_rules

    summary_title = []
    if completed_rules == 1:
        summary_title.append("%d rule matches" % completed_rules)
    elif completed_rules > 1:
        summary_title.append("%d rules match" % completed_rules)

    if potential_rules == 1:
        summary_title.append("%s potential rule" % potential_rules)
    elif potential_rules > 1:
        summary_title.append("%s potential rules" % potential_rules)

    if completed_rules == 0 and potential_rules == 0:
        summary_title.append("no rules match, no planned actions")

    summary_title = " and ".join(summary_title)

    return summary_title, summary


def _filterred_sources_for_logging(data, inplace=False):
    if not inplace:
        data = copy.deepcopy(data)

    if isinstance(data, dict):
        data.pop("node_id", None)
        data.pop("tree_id", None)
        data.pop("_links", None)
        data.pop("external_id", None)
        for key, value in list(data.items()):
            if key.endswith("url"):
                del data[key]
            else:
                data[key] = _filterred_sources_for_logging(value, inplace=True)
        return data
    elif isinstance(data, list):
        return [_filterred_sources_for_logging(elem, inplace=True) for elem in data]
    else:
        return data


def _redis_last_summary_head_sha_key(ctxt):
    installation_id = ctxt.client.auth.installation["id"]
    owner = ctxt.pull["base"]["repo"]["owner"]["id"]
    repo = ctxt.pull["base"]["repo"]["id"]
    pull_number = ctxt.pull["number"]
    return f"summary-sha~{installation_id}~{owner}~{repo}~{pull_number}"


def delete_last_summary_head_sha(ctxt):
    with utils.get_redis_for_cache() as redis:
        redis.delete(_redis_last_summary_head_sha_key(ctxt))


def get_last_summary_head_sha(ctxt):
    with utils.get_redis_for_cache() as redis:
        return redis.get(_redis_last_summary_head_sha_key(ctxt))


def save_last_summary_head_sha(ctxt):
    # NOTE(sileht): We store it only for 1 month, if we lose it it's not a big deal, as it's just
    # to avoid race conditions when too many synchronize events occur in a short period of time
    with utils.get_redis_for_cache() as redis:
        redis.set(
            _redis_last_summary_head_sha_key(ctxt),
            ctxt.pull["head"]["sha"],
            ex=60 * 60 * 24 * 31,  # 1 month
        )


def post_summary(ctxt, match, summary_check, conclusions, previous_conclusions):
    summary_title, summary = gen_summary(ctxt, match)

    summary += doc.MERGIFY_PULL_REQUEST_DOC
    summary += serialize_conclusions(conclusions)

    summary_changed = (
        not summary_check
        or summary_check["output"]["title"] != summary_title
        or summary_check["output"]["summary"] != summary
    )

    if summary_changed:
        ctxt.log.info(
            "summary changed",
            summary={
                "title": summary_title,
                "name": ctxt.SUMMARY_NAME,
                "summary": summary,
            },
            sources=_filterred_sources_for_logging(ctxt.sources),
            conclusions=conclusions,
            previous_conclusions=previous_conclusions,
        )

        ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.SUCCESS, title=summary_title, summary=summary
            )
        )
        save_last_summary_head_sha(ctxt)
    else:
        ctxt.log.info(
            "summary unchanged",
            summary={
                "title": summary_title,
                "name": ctxt.SUMMARY_NAME,
                "summary": summary,
            },
            sources=_filterred_sources_for_logging(ctxt.sources),
            conclusions=conclusions,
            previous_conclusions=previous_conclusions,
        )


def exec_action(method_name, rule, action, ctxt, missing_conditions):
    try:
        method = getattr(rule.actions[action], method_name)
        return method(ctxt, rule, missing_conditions)
    except Exception:  # pragma: no cover
        ctxt.log.error("action failed", action=action, rule=rule, exc_info=True)
        # TODO(sileht): extract sentry event id and post it, so
        # we can track it easly
        return check_api.Result(
            check_api.Conclusion.FAILURE, "action '%s' failed" % action, ""
        )


def load_conclusions(ctxt, summary_check):
    if summary_check and summary_check["output"]["summary"]:
        line = summary_check["output"]["summary"].splitlines()[-1]
        if line.startswith("<!-- ") and line.endswith(" -->"):
            return dict(
                (name, check_api.Conclusion(conclusion))
                for name, conclusion in yaml.safe_load(
                    base64.b64decode(line[5:-4].encode()).decode()
                ).items()
            )

    ctxt.log.warning(
        "previous conclusion not found in summary",
        summary_check=summary_check,
    )
    return {}


def serialize_conclusions(conclusions):
    return (
        "<!-- %s -->"
        % base64.b64encode(
            yaml.safe_dump(
                dict(
                    (name, conclusion.value) for name, conclusion in conclusions.items()
                )
            ).encode()
        ).decode()
    )


def get_previous_conclusion(previous_conclusions, name, checks):
    if name in previous_conclusions:
        return previous_conclusions[name]
    # TODO(sileht): Remove usage of legacy checks after the 15/02/2020 and if the
    # synchronization event issue is fixed
    elif name in checks:
        return check_api.Conclusion(checks[name]["conclusion"])
    return check_api.Conclusion.NEUTRAL


def run_actions(
    ctxt,
    match,
    checks,
    previous_conclusions,
):
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

    user_refresh_requested = any(
        [source["event_type"] == "refresh" for source in ctxt.sources]
    )
    forced_refresh_requested = any(
        [
            (source["event_type"] == "refresh" and source["data"]["action"] == "forced")
            for source in ctxt.sources
        ]
    )

    actions_ran = set()
    conclusions = {}

    # NOTE(sileht): We put first rules with missing conditions to do cancellation first.
    # In case of a canceled merge action and another that need to be run. We want first
    # to remove the PR from the queue and then add it back with the new config and not the
    # reverse
    matching_rules = sorted(match.matching_rules, key=lambda value: len(value[1]) == 0)

    for rule, missing_conditions in matching_rules:
        for action, action_obj in rule.actions.items():
            check_name = "Rule: %s (%s)" % (rule.name, action)

            done_by_another_action = action_obj.only_once and action in actions_ran

            if missing_conditions:
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
                action_obj.always_run
                or forced_refresh_requested
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
                    "Another %s action already ran" % action,
                    "",
                )
                message = "ignored, another has already been run"

            else:
                # NOTE(sileht): check state change so we have to run "run" or "cancel"
                report = exec_action(
                    method_name,
                    rule,
                    action,
                    ctxt,
                    missing_conditions,
                )
                message = "executed"

            if (
                report
                and report.conclusion is not check_api.Conclusion.PENDING
                and method_name == "run"
            ):
                statsd.increment("engine.actions.count", tags=["name:%s" % action])

            if report:
                if need_to_be_run and (
                    not action_obj.silent_report
                    or report.conclusion
                    not in (
                        check_api.Conclusion.SUCCESS,
                        check_api.Conclusion.CANCELLED,
                        check_api.Conclusion.PENDING,
                    )
                ):
                    external_id = (
                        check_api.USER_CREATED_CHECKS
                        if action_obj.allow_retrigger_mergify
                        else None
                    )
                    try:
                        check_api.set_check_run(
                            ctxt,
                            check_name,
                            report,
                            external_id=external_id,
                        )
                    except Exception:
                        ctxt.log.error(
                            "Fail to post check `%s`", check_name, exc_info=True
                        )
                conclusions[check_name] = report.conclusion
            else:
                # NOTE(sileht): action doesn't have report (eg:
                # comment/request_reviews/..) So just assume it succeed
                ctxt.log.error("action must return a conclusion", action=action)
                conclusions[check_name] = expected_conclusions[0]

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
                missing_conditions=missing_conditions,
                event_types=[se["event_type"] for se in ctxt.sources],
            )

    return conclusions


def handle(pull_request_rules, ctxt) -> None:
    match = pull_request_rules.get_pull_request_rule(ctxt)
    checks = dict((c["name"], c) for c in ctxt.pull_engine_check_runs)

    summary_check = checks.get(ctxt.SUMMARY_NAME)
    previous_conclusions = load_conclusions(ctxt, summary_check)

    conclusions = run_actions(ctxt, match, checks, previous_conclusions)
    post_summary(ctxt, match, summary_check, conclusions, previous_conclusions)
