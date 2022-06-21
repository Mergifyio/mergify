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
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine.clients import http
from mergify_engine.rules import types


class AssignAction(actions.Action):
    validator = {
        # NOTE: "users" is deprecated, but kept as legacy code for old config
        voluptuous.Required("users", default=list): [types.Jinja2],
        voluptuous.Required("add_users", default=list): [types.Jinja2],
        voluptuous.Required("remove_users", default=list): [types.Jinja2],
    }

    flags = (
        actions.ActionFlag.ALLOW_AS_ACTION
        | actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        | actions.ActionFlag.ALWAYS_RUN
    )

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        # NOTE: "users" is deprecated, but kept as legacy code for old config
        add_users = self.config["users"] + self.config["add_users"]
        users_to_add_parsed = await self.wanted_users(ctxt, add_users)
        assignees_to_add = list(
            users_to_add_parsed - {a["login"] for a in ctxt.pull["assignees"]}
        )
        if assignees_to_add:
            try:
                await ctxt.client.post(
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/assignees",
                    json={"assignees": assignees_to_add},
                )
            except http.HTTPClientSideError as e:  # pragma: no cover
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Unable to add assignees",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )

        users_to_remove_parsed = await self.wanted_users(
            ctxt, self.config["remove_users"]
        )
        assignees_to_remove = list(
            users_to_remove_parsed & {a["login"] for a in ctxt.pull["assignees"]}
        )
        if assignees_to_remove:
            try:
                await ctxt.client.request(
                    "DELETE",
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/assignees",
                    json={"assignees": assignees_to_remove},
                )
            except http.HTTPClientSideError as e:  # pragma: no cover
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Unable to remove assignees",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )

        if assignees_to_add or assignees_to_remove:
            await signals.send(
                ctxt.repository,
                ctxt.pull["number"],
                "action.assign",
                signals.EventAssignMetadata(
                    {"added": assignees_to_add, "removed": assignees_to_remove}
                ),
                rule.get_signal_trigger(),
            )

            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Users added/removed from assignees",
                "",
            )
        else:
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "No users added/removed from assignees",
                "",
            )

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:  # pragma: no cover
        return actions.CANCELLED_CHECK_REPORT
