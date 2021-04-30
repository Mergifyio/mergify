# -*- encoding: utf-8 -*-
#
#  Copyright © 2021 Mergify SAS
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

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import squash_pull
from mergify_engine.clients import http


class SquashAction(actions.Action):
    is_command = False
    always_run = True
    silent_report = True

    validator: typing.ClassVar[typing.Dict[typing.Any, typing.Any]] = {}

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:

        res = squash_pull.squash_pull(ctxt)

        if res.conclusion.value in (
            check_api.Result.conclusion.SUCCESS.value,
            check_api.Result.conclusion.NEUTRAL.value,
        ):
            try:
                await ctxt.client.post(
                    f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
                    json={"body": "Pull request squashed"},
                )
            except http.HTTPClientSideError as e:  # pragma: no cover
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "The squash message can't be created",
                    e.message,
                )

            await signals.send(ctxt, "action.squash")
