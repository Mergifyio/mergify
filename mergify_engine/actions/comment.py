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


class CommentAction(actions.Action):
    validator = {
        voluptuous.Required("message"): str,
    }

    def run(self, installation_id, installation_token, subscription,
            event_type, data, pull, missing_conditions):
        try:
            pull.g_pull.create_issue_comment(self.config["message"])
        except github.GithubException as e:
            pull.log.error("fail to post comment on the pull request",
                           status=e.status, error=e.data["message"])
            return ("failure", "the issue comment can't be created", "")

        return ("success", "The following message has been posted",
                self.config["message"])

    def cancel(self, installation_id, installation_token, subscription,
               event_type, data, pull, missing_conditions):  # pragma: no cover
        return self.cancelled_check_report
