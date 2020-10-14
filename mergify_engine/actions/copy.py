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

import re
from urllib import parse

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import duplicate_pull
from mergify_engine.clients import http


def Regex(value):
    try:
        re.compile(value)
    except re.error as e:
        raise voluptuous.Invalid(str(e))
    return value


class CopyAction(actions.Action):
    KIND = "copy"
    SUCCESS_MESSAGE = "Pull request copies have been created"
    FAILURE_MESSAGE = "No copy have been created"

    validator = {
        voluptuous.Required("branches", default=[]): [str],
        voluptuous.Required("regexes", default=[]): [Regex],
        voluptuous.Required("ignore_conflicts", default=True): bool,
        voluptuous.Required("label_conflicts", default="conflicts"): str,
    }

    def _copy(self, ctxt, branch_name):
        """Copy the PR to a branch.

        Returns a tuple of strings (state, reason).
        """

        # NOTE(sileht): Ensure branch exists first
        escaped_branch_name = parse.quote(branch_name, safe="")
        try:
            ctxt.client.item(f"{ctxt.base_url}/branches/{escaped_branch_name}")
        except http.HTTPStatusError as e:
            detail = "%s to branch `%s` failed: " % (
                self.KIND.capitalize(),
                branch_name,
            )
            if e.response.status_code >= 500:
                state = check_api.Conclusion.PENDING
            else:
                state = check_api.Conclusion.FAILURE
                detail += e.response.json()["message"]
            return state, detail

        # NOTE(sileht) does the duplicate have already been done ?
        new_pull = self.get_existing_duplicate_pull(ctxt, branch_name)

        # No, then do it
        if not new_pull:
            try:
                new_pull = duplicate_pull.duplicate(
                    ctxt,
                    branch_name,
                    self.config["label_conflicts"],
                    self.config["ignore_conflicts"],
                    self.KIND,
                )
            except duplicate_pull.DuplicateFailed as e:
                return (
                    check_api.Conclusion.FAILURE,
                    f"Backport to branch `{branch_name}` failed\n{e.reason}",
                )

            # NOTE(sileht): We relook again in case of concurrent duplicate
            # are done because of two events received too closely
            if not new_pull:
                new_pull = self.get_existing_duplicate_pull(ctxt, branch_name)

        if new_pull:
            return (
                check_api.Conclusion.SUCCESS,
                f"[#{new_pull['number']} {new_pull['title']}]({new_pull['html_url']}) "
                f"has been created for branch `{branch_name}`",
            )

        return (
            check_api.Conclusion.FAILURE,
            f"{self.KIND.capitalize()} to branch `{branch_name}` failed",
        )

    def run(self, ctxt, rule, missing_conditions) -> check_api.Result:
        if not config.GITHUB_APP:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Unavailable with the GitHub Action",
                "Due to GitHub Action limitation, the `copy` action/command is only "
                "available with the Mergify GitHub App.",
            )

        branches = self.config["branches"]
        if self.config["regexes"]:
            regexes = list(map(re.compile, self.config["regexes"]))
            branches.extend(
                (
                    branch["name"]
                    for branch in ctxt.client.items(f"{ctxt.base_url}/branches")
                    if any(map(lambda regex: regex.match(branch["name"]), regexes))
                )
            )

        results = [self._copy(ctxt, branch_name) for branch_name in branches]

        # Pick the first status as the final_status
        conclusion = results[0][0]
        for r in results[1:]:
            if r[0] == check_api.Conclusion.FAILURE:
                conclusion = check_api.Conclusion.FAILURE
                # If we have a failure, everything is set to fail
                break
            elif r[0] == check_api.Conclusion.SUCCESS:
                # If it was None, replace with success
                # Keep checking for a failure just in case
                conclusion = check_api.Conclusion.SUCCESS

        if conclusion == check_api.Conclusion.SUCCESS:
            message = self.SUCCESS_MESSAGE
        elif conclusion == check_api.Conclusion.FAILURE:
            message = self.FAILURE_MESSAGE
        else:
            message = "Pending"

        return check_api.Result(
            conclusion,
            message,
            "\n".join(f"* {detail}" for detail in map(lambda r: r[1], results)),
        )

    @classmethod
    def get_existing_duplicate_pull(cls, ctxt, branch_name):
        bp_branch = duplicate_pull.get_destination_branch_name(
            ctxt.pull["number"], branch_name, cls.KIND
        )
        pulls = list(
            ctxt.client.items(
                f"{ctxt.base_url}/pulls",
                base=branch_name,
                sort="created",
                state="all",
                head=f"{ctxt.pull['base']['user']['login']}:{bp_branch}",
            )
        )
        if pulls:
            return pulls[-1]
