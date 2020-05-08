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

import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine import duplicate_pull


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
        voluptuous.Required("ignore_conflicts", default=False): bool,
        voluptuous.Required("label_conflicts", default="conflicts"): str,
    }

    def _copy(self, ctxt, branch_name):
        """Copy the PR to a branch.

        Returns a tuple of strings (state, reason).
        """

        # NOTE(sileht): Ensure branch exists first
        escaped_branch_name = parse.quote(branch_name, safe="")
        try:
            ctxt.client.item(f"branches/{escaped_branch_name}")
        except httpx.HTTPError as e:
            if not e.response:
                raise
            detail = "%s to branch `%s` failed: " % (
                self.KIND.capitalize(),
                branch_name,
            )
            if e.response.status_code >= 500:
                state = None
            else:
                state = "failure"
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
                    "failure",
                    f"Backport to branch `{branch_name}` failed\n{e.reason}",
                )

            # NOTE(sileht): We relook again in case of concurrent duplicate
            # are done because of two events received too closely
            if not new_pull:
                new_pull = self.get_existing_duplicate_pull(ctxt, branch_name)

        if new_pull:
            return (
                "success",
                f"[#{new_pull['number']} {new_pull['title']}]({new_pull['html_url']}) "
                f"has been created for branch `{branch_name}`",
            )

        return (
            "failure",
            f"{self.KIND.capitalize()} to branch `{branch_name}` failed",
        )

    def run(self, ctxt, rule, missing_conditions):
        branches = self.config["branches"]
        if self.config["regexes"]:
            regexes = list(map(re.compile, self.config["regexes"]))
            branches.extend(
                (
                    branch["name"]
                    for branch in ctxt.client.items("branches")
                    if any(map(lambda regex: regex.match(branch["name"]), regexes))
                )
            )

        results = [self._copy(ctxt, branch_name) for branch_name in branches]

        # Pick the first status as the final_status
        final_status = results[0][0]
        for r in results[1:]:
            if r[0] == "failure":
                final_status = "failure"
                # If we have a failure, everything is set to fail
                break
            elif r[0] == "success":
                # If it was None, replace with success
                # Keep checking for a failure just in case
                final_status = "success"

        if final_status == "success":
            message = self.SUCCESS_MESSAGE
        elif final_status == "failure":
            message = self.FAILURE_MESSAGE
        else:
            message = "Pending"

        return (
            final_status,
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
                "pulls",
                base=branch_name,
                sort="created",
                state="all",
                head=f"{ctxt.pull['head']['user']['login']}:{bp_branch}",
            )
        )
        if pulls:
            return pulls[-1]
