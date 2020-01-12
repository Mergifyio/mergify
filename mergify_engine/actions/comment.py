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
from mergify_engine import config


class CommentAction(actions.Action):
    validator = {voluptuous.Required("message"): str}

    def deprecated_already_done_protection(self, pull):
        # TODO(sileht): drop this in 2 months (February 2020)
        for comment in pull.g_pull.get_issue_comments():
            if (
                comment.user.id == config.BOT_USER_ID
                and comment.body == self.config["message"]
            ):
                return True
        return False

    def run(
        self,
        installation_id,
        installation_token,
        event_type,
        data,
        pull,
        missing_conditions,
    ):
        try:
            pull.g_pull.create_issue_comment(self.config["message"])
        except github.GithubException as e:  # pragma: no cover
            pull.log.error(
                "fail to post comment on the pull request",
                status=e.status,
                error=e.data["message"],
            )
