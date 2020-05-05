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

import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine import context


MSG = "This pull request has been automatically closed by Mergify."


class CloseAction(actions.Action):
    only_once = True
    validator = {voluptuous.Required("message", default=MSG): str}

    def run(self, ctxt, missing_conditions):
        if ctxt.pull["state"] == "close":
            return ("success", "Pull request is already closed", "")

        try:
            ctxt.client.patch(f"pulls/{ctxt.pull['number']}", json={"state": "close"})
        except httpx.HTTPClientSideError as e:  # pragma: no cover
            return ("failure", "Pull request can't be closed", e.message)

        try:
            message = ctxt.pull_request.render_template(self.config["message"])
        except context.RenderTemplateFailure as rmf:
            return (
                "failure",
                "Invalid close message",
                str(rmf),
            )

        try:
            ctxt.client.post(
                f"issues/{ctxt.pull['number']}/comments", json={"body": message},
            )
        except httpx.HTTPClientSideError as e:  # pragma: no cover
            return ("failure", "The close message can't be created", e.message)

        return ("success", "The pull request has been closed", message)
