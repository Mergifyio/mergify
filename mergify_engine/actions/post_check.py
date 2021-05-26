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

import typing

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
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
            default="'{{ check_rule_name }}' {% if check_succeed %}succeed{% else %}failed{% endif %}",  # noqa:FS003
        ): CheckRunJinja2,
        voluptuous.Required(
            "summary", default="{{ check_conditions }}"
        ): CheckRunJinja2,
    }

    always_run = True
    allow_retrigger_mergify = True

    async def _post(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        # TODO(sileht): Don't run it if conditions contains the rule itself, as it can
        # created an endless loop of events.

        if not ctxt.subscription.has_feature(subscription.Features.CUSTOM_CHECKS):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Custom checks are disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        extra_variables: typing.Dict[str, typing.Union[str, bool]] = {
            "check_rule_name": rule.name,
            "check_succeed": rule.conditions.match,
            "check_conditions": rule.conditions.get_summary(),
        }
        try:
            title = await ctxt.pull_request.render_template(
                self.config["title"],
                extra_variables,
            )
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid title template",
                str(rmf),
            )

        try:
            summary = await ctxt.pull_request.render_template(
                self.config["summary"], extra_variables
            )
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid summary template",
                str(rmf),
            )

        await signals.send(ctxt, "action.post_check")
        if rule.conditions.match:
            return check_api.Result(check_api.Conclusion.SUCCESS, title, summary)
        else:
            return check_api.Result(check_api.Conclusion.FAILURE, title, summary)

    run = _post
    cancel = _post
