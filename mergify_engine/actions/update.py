# -*- encoding: utf-8 -*-
#
#  Copyright © 2020 Mehdi Abaakouk <sileht@mergify.io>
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
from mergify_engine import branch_updater
from mergify_engine import check_api


class UpdateAction(actions.Action):
    is_command = True

    always_run = True

    silent_report = True

    validator: typing.ClassVar[typing.Dict] = {}

    @staticmethod
    def run(ctxt, rule, missing_conditions) -> check_api.Result:
        if ctxt.is_behind:
            try:
                branch_updater.update_with_api(ctxt)
            except branch_updater.BranchUpdateFailure as e:
                return check_api.Result(
                    check_api.Conclusion.FAILURE, "Branch update failed", str(e)
                )
            else:
                return check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    "Branch has been successfully updated",
                    "",
                )
        else:
            return check_api.Result(
                check_api.Conclusion.SUCCESS, "Branch already up to date", ""
            )
