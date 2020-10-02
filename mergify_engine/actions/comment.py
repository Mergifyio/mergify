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
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine.clients import http
from mergify_engine.rules import types


class CommentAction(actions.Action):
    validator = {voluptuous.Required("message"): types.Jinja2}

    silent_report = True

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        try:
            message = ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Invalid comment message",
                str(rmf),
            )

        try:
            ctxt.client.post(
                f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                json={"body": message},
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            return check_api.Result(
                check_api.Conclusion.PENDING,
                "Unable to post comment",
                f"GitHub error: [{e.status_code}] `{e.message}`",
            )
        return check_api.Result(check_api.Conclusion.SUCCESS, "Comment posted", message)
