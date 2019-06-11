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

from urllib import parse

import daiquiri

import github

import voluptuous

from mergify_engine import actions
from mergify_engine import backports

LOG = daiquiri.getLogger(__name__)


class BackportAction(actions.Action):
    validator = {voluptuous.Required("branches", default=[]): [str]}

    def run(self, installation_id, installation_token,
            event_type, data, pull, missing_conditions):
        if not pull.g_pull.merged:
            return None, "Waiting for the pull request to get merged", ""

        state = "success"
        detail = "The following pull requests have been created: "
        for branch_name in self.config['branches']:
            try:
                branch = pull.g_pull.base.repo.get_branch(
                    parse.quote(branch_name, safe="")
                )
            except github.GithubException as e:  # pragma: no cover
                LOG.error("backport: fail to get branch",
                          pull_request=pull,
                          error=e.data["message"])

                state = "failure"
                detail += ("\n* backport to branch `%s` has failed" %
                           branch_name)
                if e.status == 404:
                    detail += ": the branch does not exists"
                continue

            # NOTE(sileht) does the backport have already been done ?
            new_pull = self.get_existing_backport_pull(pull, branch)

            # No, then do it
            if not new_pull:
                new_pull = backports.backport(pull, branch, installation_token)

                # NOTE(sileht): We relook again in case of concurrent backport
                # are done because of two events received too closely
                if not new_pull:
                    new_pull = self.get_existing_backport_pull(pull, branch)

            if new_pull:
                detail += "\n* [#%d %s](%s)" % (new_pull.number,
                                                new_pull.title,
                                                new_pull.html_url)
            else:  # pragma: no cover
                state = "failure"
                detail += ("\n* backport to branch `%s` has failed" %
                           branch_name)

        return state, "Backports have been created", detail

    @staticmethod
    def get_existing_backport_pull(pull, branch):
        bp_branch = "mergify/bp/%s/pr-%s" % (branch.name, pull.g_pull.number)
        # NOTE(sileht): Github looks buggy here, head= doesn't work as expected
        pulls = list(
            p for p in pull.g_pull.base.repo.get_pulls(base=branch.name,
                                                       sort="created",
                                                       state="all")
            if p.head.ref == bp_branch
        )
        if pulls:
            return pulls[-1]

    def cancel(self, installation_id, installation_token,
               event_type, data, pull, missing_conditions):  # pragma: no cover
        return self.cancelled_check_report
