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

import yaml

from datadog import statsd
from mergify_engine import check_api
from mergify_engine import doc

PULL_REQUEST_EMBEDDED_CHECK_BACKLOG = 10

SUMMARY_NAME = "Summary"

NOT_APPLICABLE_TEMPLATE = """<details>
<summary>Rules not applicable to this pull request:</summary>
%s
</details>"""


def find_embedded_pull(pull):
    # NOTE(sileht): We are looking for a pull request that have been merged
    # very recently and have commit sha in common with current pull request.
    expected_commits = [c.sha for c in pull.g_pull.get_commits()]
    pulls = pull.g_pull.base.repo.get_pulls(state="closed", base=pull.g_pull.base.ref)[
        0:PULL_REQUEST_EMBEDDED_CHECK_BACKLOG
    ]

    for p_other in pulls:
        if p_other.number == pull.g_pull.number:
            continue
        commits = [c.sha for c in p_other.get_commits()]
        commits_not_found = [c for c in expected_commits if c not in commits]
        if not commits_not_found:
            return p_other


def get_already_merged_summary(pull, sources, match):
    for source in sources:
        if (
            source["event_type"] != "pull_request"
            or source["data"]["action"] != "closed"
            or not pull.g_pull.merged
        ):
            return ""

    action_merge_found = False
    action_merge_found_in_active_rule = False

    for rule, missing_conditions in match.matching_rules:
        if "merge" in rule["actions"]:
            action_merge_found = True
            if not missing_conditions:
                action_merge_found_in_active_rule = True

    # We already have a fully detailled status in the rule associated with the
    # action merge
    if not action_merge_found or action_merge_found_in_active_rule:
        return ""

    other_pr = find_embedded_pull(pull)
    if other_pr:
        return (
            "⚠️ The pull request has been closed by GitHub"
            "because its commits are also part of #%d\n\n" % other_pr.number
        )
    else:
        return (
            "⚠️ The pull request has been merged manually by "
            "@%s\n\n" % pull.g_pull.merged_by.login
        )


def gen_summary_rules(rules):
    summary = ""
    for rule, missing_conditions in rules:
        if rule["hidden"]:
            continue
        summary += "#### Rule: %s" % rule["name"]
        summary += " (%s)" % ", ".join(rule["actions"])
        for cond in rule["conditions"]:
            checked = " " if cond in missing_conditions else "X"
            summary += "\n- [%s] `%s`" % (checked, cond)
        summary += "\n\n"
    return summary


def gen_summary(pull, sources, match):
    summary = ""
    summary += get_already_merged_summary(pull, sources, match)
    summary += gen_summary_rules(match.matching_rules)
    ignored_rules = len(list(filter(lambda x: not x[0]["hidden"], match.ignored_rules)))

    commit_message = pull.get_merge_commit_message()
    if commit_message:
        summary += "<hr />The merge or squash commit message will be:\n\n"
        summary += "```\n"
        summary += commit_message["commit_title"] + "\n\n"
        summary += commit_message["commit_message"] + "\n"
        summary += "```\n\n"

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


def post_summary(pull, sources, match, summary_check, conclusions):
    summary_title, summary = gen_summary(pull, sources, match)

    summary += doc.MERGIFY_PULL_REQUEST_DOC
    summary += serialize_conclusions(conclusions)

    summary_changed = (
        not summary_check
        or summary_check.output["title"] != summary_title
        or summary_check.output["summary"] != summary
    )

    if summary_changed:
        pull.log.debug(
            "summary changed",
            summary={"title": summary_title, "name": SUMMARY_NAME, "summary": summary},
        )
        check_api.set_check_run(
            pull.g_pull,
            SUMMARY_NAME,
            "completed",
            "success",
            output={"title": summary_title, "summary": summary},
        )
    else:
        pull.log.debug(
            "summary unchanged",
            summary={"title": summary_title, "name": SUMMARY_NAME, "summary": summary},
        )


def exec_action(method_name, rule, action, pull, sources, missing_conditions):
    try:
        method = getattr(rule["actions"][action], method_name)
        return method(pull, sources, missing_conditions)
    except Exception:  # pragma: no cover
        pull.log.error(
            "action failed", action=action, rule=rule, pull_request=pull, exc_info=True
        )
        # TODO(sileht): extract sentry event id and post it, so
        # we can track it easly
        return "failure", "action '%s' failed" % action, " "


def load_conclusions(pull, summary_check):
    if not summary_check:
        return {}

    if summary_check.output["summary"]:
        line = summary_check.output["summary"].splitlines()[-1]
        if line.startswith("<!-- ") and line.endswith(" -->"):
            return yaml.safe_load(base64.b64decode(line[5:-4].encode()).decode())

    pull.log.warning(
        "previous conclusion not found in summary",
        pull_request=pull,
        summary_check=summary_check,
    )
    return {"deprecated_summary": True}


def serialize_conclusions(conclusions):
    return (
        "<!-- %s -->" % base64.b64encode(yaml.safe_dump(conclusions).encode()).decode()
    )


def get_previous_conclusion(previous_conclusions, name, checks):
    if name in previous_conclusions:
        return previous_conclusions[name]
    # TODO(sileht): Remove usage of legacy checks after the 15/02/2020 and if the
    # synchrnozation event issue is fixed
    elif name in checks:
        return checks[name].conclusion
    return "neutral"


def run_actions(
    pull, sources, match, checks, previous_conclusions,
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

    actions_ran = set()
    conclusions = {}
    # Run actions
    for rule, missing_conditions in match.matching_rules:
        for action in rule["actions"]:
            check_name = "Rule: %s (%s)" % (rule["name"], action)

            previous_conclusion = get_previous_conclusion(
                previous_conclusions, check_name, checks
            )

            done_by_another_action = (
                rule["actions"][action].only_once and action in actions_ran
            )

            if missing_conditions:
                method_name = "cancel"
                expected_conclusions = ["neutral", "cancelled"]
            else:
                method_name = "run"
                expected_conclusions = ["success", "failure"]
                actions_ran.add(action)

            # TODO(sileht): Backward compatibility, drop this in 2 months (February 2020)
            if (
                previous_conclusions.get("deprecated_summary", False)
                and action == "comment"
            ):
                deprecated_done_in_the_past = rule["actions"][
                    action
                ].deprecated_already_done_protection(pull)
            else:
                deprecated_done_in_the_past = None

            done_in_the_past = not rule["actions"][action].always_run and (
                previous_conclusion in expected_conclusions
                or deprecated_done_in_the_past
            )

            if done_in_the_past:
                report = None
                message = "ignored, already in expected state: %s/%s" % (
                    method_name,
                    previous_conclusion,
                )

            elif done_by_another_action:
                # NOTE(sileht) We can't run two action merge for example,
                # This assumes the action produce a report
                report = ("success", "Another %s action already ran" % action, "")
                message = "ignored, another action `%s` has already been run" % action

            else:
                # NOTE(sileht): check state change so we have to run "run" or "cancel"
                report = exec_action(
                    method_name, rule, action, pull, sources, missing_conditions,
                )
                message = "`%s` executed" % method_name

            if report and report[0] is not None and method_name == "run":
                statsd.increment("engine.actions.count", tags=["name:%s" % action])

            if report:
                conclusion, title, summary = report
                status = "completed" if conclusion else "in_progress"
                try:
                    check_api.set_check_run(
                        pull.g_pull,
                        check_name,
                        status,
                        conclusion,
                        output={"title": title, "summary": summary},
                    )
                except Exception:
                    pull.log.error("Fail to post check `%s`", check_name, exc_info=True)
                conclusions[check_name] = conclusion
            else:
                # NOTE(sileht): action doesn't have report (eg:
                # comment/request_reviews/..) So just assume it succeed
                conclusions[check_name] = expected_conclusions[0]

            pull.log.info(
                "action evaluation: %s",
                message,
                report=report,
                previous_conclusion=previous_conclusion,
                conclusion=conclusions[check_name],
                check_name=check_name,
                missing_conditions=missing_conditions,
                event_types=[se["event_type"] for se in sources],
            )

    return conclusions


def handle(pull_request_rules, pull, sources):
    match = pull_request_rules.get_pull_request_rule(pull)
    checks = dict(
        (c.name, c) for c in check_api.get_checks(pull.g_pull, mergify_only=True)
    )

    summary_check = checks.get(SUMMARY_NAME)
    previous_conclusions = load_conclusions(pull, summary_check)

    conclusions = run_actions(pull, sources, match, checks, previous_conclusions,)

    post_summary(pull, sources, match, summary_check, conclusions)
