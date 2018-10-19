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

import github

import voluptuous

from mergify_engine import actions
from mergify_engine import backports


class BackportAction(actions.Action):
    validator = {voluptuous.Required("branches", default=[]): [str]}

    def run(self, installation_id, installation_token, subscription,
            event_type, data, pull, missing_conditions):
        if not pull.g_pull.merged:
            return None, "Waiting for the pull request to get merged", ""

        state = "success"
        detail = "The following pull requests have been created: "
        for branch_name in self.config['branches']:
            try:
                branch = pull.g_pull.base.repo.get_branch(branch_name)
            except github.GithubException as e:
                pull.log.error("backport: fail to get branch",
                               error=e.data["message"])

                state = "failure"
                detail += ("\n* backport to branch `%s` has failed" %
                           branch_name)
                if e.status == 404:
                    detail += ": the branch does not exists"
                continue

            new_pull = backports.backport(pull, branch, installation_token)
            if new_pull:
                detail += "\n* [#%d %s](%s)" % (new_pull.number,
                                                new_pull.title,
                                                new_pull.html_url)
            else:  # pragma: no cover
                state = "failure"
                detail += ("\n* backport to branch `%s` has failed" %
                           branch_name)

        return state, "Backports have been created", detail

    def cancel(self, installation_id, installation_token, subscription,
               event_type, data, pull, missing_conditions):  # pragma: no cover
        return self.cancelled_check_report
