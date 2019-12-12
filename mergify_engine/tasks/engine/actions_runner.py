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


import daiquiri

import pkg_resources

import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import doc
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)

mergify_rule_path = pkg_resources.resource_filename(
    __name__, "../../data/default_pull_request_rules.yml"
)

with open(mergify_rule_path, "r") as f:
    MERGIFY_RULE = yaml.safe_load(f.read())


PULL_REQUEST_EMBEDDED_CHECK_BACKLOG = 10

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


def get_already_merged_summary(event_type, data, pull, match):
    if (
        event_type != "pull_request"
        or data["action"] != "closed"
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


def gen_summary(event_type, data, pull, match):
    summary = ""
    summary += get_already_merged_summary(event_type, data, pull, match)
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

    summary += doc.MERGIFY_PULL_REQUEST_DOC

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


def post_summary(event_type, data, pull, match, checks):
    summary_title, summary = gen_summary(event_type, data, pull, match)

    summary_name = "Summary"
    summary_check = checks.get(summary_name)

    summary_changed = (
        not summary_check
        or summary_check.output["title"] != summary_title
        or summary_check.output["summary"] != summary
    )

    if summary_changed:
        LOG.debug(
            "summary changed",
            summary={"title": summary_title, "name": summary_name, "summary": summary},
        )
        check_api.set_check_run(
            pull.g_pull,
            summary_name,
            "completed",
            "success",
            output={"title": summary_title, "summary": summary},
        )
    else:
        LOG.debug(
            "summary unchanged",
            summary={"title": summary_title, "name": summary_name, "summary": summary},
        )


def exec_action(
    method_name,
    rule,
    action,
    installation_id,
    installation_token,
    event_type,
    data,
    pull,
    missing_conditions,
):
    try:
        method = getattr(rule["actions"][action], method_name)
        return method(
            installation_id,
            installation_token,
            event_type,
            data,
            pull,
            missing_conditions,
        )
    except Exception:  # pragma: no cover
        LOG.error(
            "action failed", action=action, rule=rule, pull_request=pull, exc_info=True
        )
        # TODO(sileht): extract sentry event id and post it, so
        # we can track it easly
        return "failure", "action '%s' failed" % action, " "


def run_actions(
    installation_id, installation_token, event_type, data, pull, match, checks
):

    actions_ran = []
    # Run actions
    for rule, missing_conditions in match.matching_rules:
        for action in rule["actions"]:
            check_name = "Rule: %s (%s)" % (rule["name"], action)
            prev_check = checks.get(check_name)

            if missing_conditions:
                if not prev_check:
                    LOG.info(
                        "action evaluation: nothing to cancel",
                        check_name=check_name,
                        pull_request=pull,
                        missing_conditions=missing_conditions,
                    )
                    continue
                method_name = "cancel"
                expected_conclusion = ["cancelled", "neutral"]
            else:
                method_name = "run"
                expected_conclusion = ["success", "failure"]

            already_run = (
                prev_check
                and prev_check.conclusion in expected_conclusion
                and event_type != "refresh"
            )
            if already_run:
                if method_name == "run":
                    actions_ran.append(action)
                LOG.info(
                    "action evaluation: already in expected state",
                    conclusion=(
                        prev_check.conclusion if prev_check else "no-previous-check"
                    ),
                    check_name=check_name,
                    pull_request=pull,
                    missing_conditions=missing_conditions,
                )
                continue

            # NOTE(sileht) We can't run two action merge for example
            if rule["actions"][action].only_once and action in actions_ran:
                LOG.info(
                    "action evaluation: skipped another action %s "
                    "has already been run",
                    action,
                    check_name=check_name,
                    pull_request=pull,
                    missing_conditions=missing_conditions,
                )
                report = ("success", "Another %s action already ran" % action, "")
            else:
                report = exec_action(
                    method_name,
                    rule,
                    action,
                    installation_id,
                    installation_token,
                    event_type,
                    data,
                    pull,
                    missing_conditions,
                )
                actions_ran.append(action)

            if report:
                conclusion, title, summary = report
                status = "completed" if conclusion else "in_progress"
                check_api.set_check_run(
                    pull.g_pull,
                    check_name,
                    status,
                    conclusion,
                    output={"title": title, "summary": summary},
                )

            LOG.info(
                "action evaluation: done",
                report=report,
                check_name=check_name,
                pull_request=pull,
                missing_conditions=missing_conditions,
            )


@app.task
def handle(installation_id, pull_request_rules_raw, event_type, data):

    installation_token = utils.get_installation_token(installation_id)
    if not installation_token:
        return

    # Some mandatory rules
    pull_request_rules_raw["rules"].extend(MERGIFY_RULE["rules"])

    pull_request_rules = rules.PullRequestRules(**pull_request_rules_raw)
    pull = mergify_pull.MergifyPull.from_raw(
        installation_id, installation_token, data["pull_request"]
    )
    match = pull_request_rules.get_pull_request_rule(pull)
    checks = dict(
        (c.name, c)
        for c in check_api.get_checks(pull.g_pull)
        if c._rawData["app"]["id"] == config.INTEGRATION_ID
    )

    post_summary(event_type, data, pull, match, checks)

    run_actions(
        installation_id, installation_token, event_type, data, pull, match, checks
    )
