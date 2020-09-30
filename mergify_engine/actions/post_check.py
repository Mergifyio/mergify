# -*- encoding: utf-8 -*-
#
#  Copyright © 2020 Mergify SAS
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

import voluptuous

from mergify_engine import actions
from mergify_engine import context
from mergify_engine import subscription
from mergify_engine.rules import types


def CheckRunJinja2(v):
    return types.Jinja2(
        v,
        {
            "check_rule_name": "whatever",
            "check_succeed": True,
            "check_conditions": "the expected condition conditions",
        },
    )


class PostCheckAction(actions.Action):
    validator = {
        voluptuous.Required(
            "title",
            default="'{{ check_rule_name }}' {% if check_succeed %}succeed{% else %}failed{% endif %}",
        ): CheckRunJinja2,
        voluptuous.Required(
            "summary", default="{{ check_conditions }}"
        ): CheckRunJinja2,
    }

    always_run = True
    allow_retrigger_mergify = True

    def _post(self, ctxt, rule, missing_conditions):
        # TODO(sileht): Don't run it if conditions contains the rule itself, as it can
        # created an endless loop of events.

        if not ctxt.subscription.has_feature(subscription.Features.CUSTOM_CHECKS):
            return (
                "action_required",
                "Custom checks are disabled",
                f"⚠  The [subscription](https://dashboard.mergify.io/installation/{ctxt.client.auth.installation['id']}/subscription) needed to be updated to enable them.",
            )

        check_succeed = not bool(missing_conditions)
        check_conditions = ""
        for cond in rule["conditions"]:
            checked = " " if cond in missing_conditions else "X"
            check_conditions += f"\n- [{checked}] `{cond}`"

        extra_variables = {
            "check_rule_name": rule["name"],
            "check_succeed": check_succeed,
            "check_conditions": check_conditions,
        }
        try:
            title = ctxt.pull_request.render_template(
                self.config["title"],
                extra_variables,
            )
        except context.RenderTemplateFailure as rmf:
            return (
                "failure",
                "Invalid title template",
                str(rmf),
            )

        try:
            summary = ctxt.pull_request.render_template(
                self.config["summary"], extra_variables
            )
        except context.RenderTemplateFailure as rmf:
            return (
                "failure",
                "Invalid summary template",
                str(rmf),
            )

        return ("failure" if missing_conditions else "success", title, summary)

    run = _post
    cancel = _post
