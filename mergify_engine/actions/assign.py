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
from mergify_engine.clients import http
from mergify_engine.rules import types


class AssignAction(actions.Action):
    validator = {
        # NOTE: "users" is deprecated, but kept as legacy code for old config
        voluptuous.Required("users", default=[]): [types.Jinja2],
        voluptuous.Required("add_users", default=[]): [types.Jinja2],
        voluptuous.Required("remove_users", default=[]): [types.Jinja2],
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
        if self.config["users"]:
            await self._add_assignees(ctxt, self.config["users"])

        if self.config["add_users"]:
            await self._add_assignees(ctxt, self.config["add_users"])

        if self.config["remove_users"]:
            await self._remove_assignees(ctxt, self.config["remove_users"])

        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            "Users added/removed from assignees",
            "",
        )

    async def _add_assignees(
        self, ctxt: context.Context, users_to_add: typing.List[str]
    ) -> check_api.Result:
        assignees = await self.wanted_users(ctxt, users_to_add)

        if assignees:
            try:
                await ctxt.client.post(
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/assignees",
                    json={"assignees": assignees},
                )
            except http.HTTPClientSideError as e:  # pragma: no cover
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Unable to add assignees",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )

            await signals.send(ctxt, "action.assign.added")
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Assignees added",
                ", ".join(assignees),
            )
        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            "Empty users list",
            "No user added to assignees",
        )

    async def _remove_assignees(
        self, ctxt: context.Context, users_to_remove: typing.List[str]
    ) -> check_api.Result:
        assignees = await self.wanted_users(ctxt, users_to_remove)

        if assignees:
            try:
                await ctxt.client.request(
                    "DELETE",
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/assignees",
                    json={"assignees": assignees},
                )
            except http.HTTPClientSideError as e:  # pragma: no cover
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Unable to remove assignees",
                    f"GitHub error: [{e.status_code}] `{e.message}`",
                )

            await signals.send(ctxt, "action.assign.removed")
            return check_api.Result(
                check_api.Conclusion.SUCCESS,
                "Assignees removed",
                ", ".join(assignees),
            )
        return check_api.Result(
            check_api.Conclusion.SUCCESS,
            "Empty users list",
            "No user removed from assignees",
        )
