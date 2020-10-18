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
import asyncio
import typing

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import github_events


class RefreshAction(actions.Action):
    is_command = True
    is_action = False
    validator: typing.ClassVar[dict] = {}

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        asyncio.run(github_events.send_refresh(ctxt.pull))
        return check_api.Result(
            check_api.Conclusion.SUCCESS, title="Pull request refreshed", summary=""
        )
