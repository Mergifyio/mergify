# -*- encoding: utf-8 -*-
#
#  Copyright © 2019 Mehdi Abaakouk <sileht@sileht.net>
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

from mergify_engine import actions
from mergify_engine import branch_updater


class RebaseAction(actions.Action):
    is_command = True

    validator = {}

    @staticmethod
    def run(ctxt, rule, missing_conditions):
        try:
            branch_updater.update_with_git(ctxt, "rebase")
        except branch_updater.BranchUpdateFailure as e:
            return "failure", "Branch rebase failed", str(e)
        else:
            return "success", "Branch has been successfully rebased", ""
