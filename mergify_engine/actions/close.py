# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import jinja2_utils
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.clients import http
from mergify_engine.rules import types


MSG = "This pull request has been automatically closed by Mergify."


class CloseAction(actions.Action):
    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALWAYS_SEND_REPORT
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.DISALLOW_RERUN_ON_OTHER_RULES
    )
    validator = {voluptuous.Required("message", default=MSG): types.Jinja2}

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if ctxt.closed:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Pull request is already closed", ""
            )

        try:
            await ctxt.client.patch(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}", json={"state": "close"}
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.FAILURE, "Pull request can't be closed", e.message
            )

        try:
            message = await jinja2_utils.render_template(
                ctxt.pull_request, self.config["message"]
            )
        except jinja2_utils.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid close message",
                str(rmf),
            )

        try:
            await ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                json={"body": message},
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "The close message can't be created",
                e.message,
            )

        await signals.send(ctxt, "action.close")
        return check_api.Result(
            check_api.Conclusion.SUCCESS, "The pull request has been closed", message
        )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
