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


class DeleteHeadBranchAction(actions.Action):
    validator = {
        voluptuous.Required("on", default="merge"):
        voluptuous.Any("merge", "close"),
    }

    def __call__(self, installation_id, installation_token, subscription,
                 event_type, data, pull):
        if pull.g_pull.head.repo != pull.g_pull.base.repo:
            return
        if ((self.config["on"] == "merge" and pull.g_pull.merged) or
                (self.config["on"] == "close" and
                 pull.g_pull.state == "closed")):
            try:
                pull.g_pull.head.repo._requester.requestJsonAndCheck(
                    "DELETE",
                    pull.g_pull.base.repo.url + "/git/refs/heads/" +
                    pull.g_pull.head.ref)
            except github.GithubException as e:
                # if e.status == 404:
                #     return
                pull.log.error("Unable to delete head branch",
                               status=e.status, error=e.data["message"])
                return ("failure", "Unable to delete the head branch", " ")
            return ("success", "Branch `%s` have been deleted" %
                    pull.g_pull.head.ref, " ")
