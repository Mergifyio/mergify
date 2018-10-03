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

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)


def post_summary(pull, match, checks):
    # Set the summary
    summary_name = "Mergify Summary"
    summary = "The following rules match this pull request:"

    completed_rules = 0
    for rule, missing_conditions in match.matching_rules:
        summary += "\n\n### %s" % rule['name']
        summary += " (actions: %s)" % ", ".join(rule['actions'])
        for cond in rule['conditions']:
            checked = (":heavy_minus_sign:"
                       if cond in missing_conditions else
                       ":heavy_check_mark:")
            summary += "\n\n%s  %s" % (checked, cond)
        if not missing_conditions:
            completed_rules += 1

    summary_title = "%s rules matches" % len(match.matching_rules)
    if completed_rules > 0:
        summary_title += ", %s applied" % completed_rules
    summary_check = checks.get(summary_name)
    summary_changed = (not summary_check or
                       summary_check.output["title"] != summary_title or
                       summary_check.output["summary"] != summary)

    if summary_changed:
        check_api.set_check_run(
            pull.g_pull, summary_name, "completed", "success",
            output={"title": summary_title, "summary": summary})


def run_action(rule, action, check_name, prev_check, installation_id,
               installation_token, subscription, event_type, data, pull):
    try:
        return rule['actions'][action](
            installation_id, installation_token,
            subscription, event_type, data, pull)
    except Exception as e:
        pull.log.error("action failed", action=action, rule=rule,
                       exc_info=True)
        # TODO(sileht): extract sentry event id and post it, so
        # we can track it easly
        return "failure", "action '%s' have failed" % action


def run_actions(installation_id, installation_token, subscription,
                event_type, data, pull, match, checks):

    # Run actions
    for rule, missing_conditions in match.matching_rules:
        for action in rule['actions']:
            check_name = "Rule: %s (%s)" % (rule['name'], action)
            title = "Result of action %s for rule '%s'" % (action,
                                                           rule['name'])
            prev_check = checks.get(check_name)

            if missing_conditions:
                # NOTE(sileht): The rule was matching before, but it doesn't
                # anymore, since we can't remove checks, put them in cancelled
                # state
                cancel_in_progress = rule["actions"][action].cancel_in_progress
                if (cancel_in_progress and prev_check and
                        prev_check.status == "in_progress"):
                    summary = ("The rule doesn't match anymore, this action "
                               "has been cancelled")
                    check_api.set_check_run(
                        pull.g_pull, check_name, "completed", "cancelled",
                        output={"title": title, "summary": summary})
                continue

            # NOTE(sileht): actions already done
            if prev_check and prev_check.conclusion:
                continue

            conclusion, summary = run_action(
                rule, action, check_name, prev_check,
                installation_id, installation_token, subscription,
                event_type, data, pull
            )

            if conclusion:
                status = "completed"
            else:
                status = "in_progress"

            check_api.set_check_run(
                pull.g_pull, check_name, status, conclusion,
                output={"title": title, "summary": summary})


@app.task
def handle(installation_id, installation_token, subscription,
           mergify_config, event_type, data, pull_raw):
    pull = mergify_pull.MergifyPull.from_raw(
        installation_id,
        installation_token,
        pull_raw)
    match = mergify_config['pull_request_rules'].get_pull_request_rule(pull)
    checks = dict((c.name, c) for c in check_api.get_checks(pull.g_pull)
                  if c._rawData['app']['id'] == config.INTEGRATION_ID)

    post_summary(pull, match, checks)

    run_actions(installation_id, installation_token, subscription,
                event_type, data, pull, match, checks)
