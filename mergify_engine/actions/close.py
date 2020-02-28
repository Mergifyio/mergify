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


MSG = "This pull request has been automatically closed by Mergify."


class CloseAction(actions.Action):
    only_once = True
    validator = {voluptuous.Required("message", default=MSG): str}

    def run(self, pull, sources, missing_conditions):
        if pull.state == "close":
            return ("success", "Pull request is already closed", "")

        try:
            pull.g_pull.edit(state="close")
        except github.GithubException as e:  # pragma: no cover
            if e.status >= 500:
                raise
            return ("failure", "Pull request can't be closed", e.data["message"])

        try:
            pull.g_pull.create_issue_comment(self.config["message"])
        except github.GithubException as e:  # pragma: no cover
            if e.status >= 500:
                raise
            return ("failure", "The close message can't be created", e.data["message"])

        return ("success", "The pull request has been closed", self.config["message"])
