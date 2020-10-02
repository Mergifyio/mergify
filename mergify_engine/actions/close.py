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
from mergify_engine.clients import http
from mergify_engine.rules import types


MSG = "This pull request has been automatically closed by Mergify."


class CloseAction(actions.Action):
    only_once = True
    validator = {voluptuous.Required("message", default=MSG): types.Jinja2}

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        if ctxt.pull["state"] == "close":
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Pull request is already closed", ""
            )

        try:
            ctxt.client.patch(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}", json={"state": "close"}
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.FAILURE, "Pull request can't be closed", e.message
            )

        try:
            message = ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid close message",
                str(rmf),
            )

        try:
            ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                json={"body": message},
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "The close message can't be created",
                e.message,
            )

        return check_api.Result(
            check_api.Conclusion.SUCCESS, "The pull request has been closed", message
        )
