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

import github
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
    }

    def _copy(self, pull, branch_name):
        """Copy the PR to a branch.

        Returns a tuple of strings (state, reason).
        """
        try:
            branch = pull.g_pull.base.repo.get_branch(parse.quote(branch_name, safe=""))
        except github.GithubException as e:
            if e.status >= 500:
                state = None
            else:
                state = "failure"
            detail = "%s to branch `%s` failed: " % (
                self.KIND.capitalize(),
                branch_name,
            )
            detail += e.data["message"]
            return state, detail

        # NOTE(sileht) does the duplicate have already been done ?
        new_pull = self.get_existing_duplicate_pull(pull, branch)

        # No, then do it
        if not new_pull:
            try:
                new_pull = duplicate_pull.duplicate(
                    pull, branch, self.config["ignore_conflicts"], self.KIND,
                )
            except duplicate_pull.DuplicateFailed as e:
                return (
                    "failure",
                    f"Backport to branch `{branch_name}` failed\n{e.reason}",
                )

            # NOTE(sileht): We relook again in case of concurrent duplicate
            # are done because of two events received too closely
            if not new_pull:
                new_pull = self.get_existing_duplicate_pull(pull, branch)

        if new_pull:
            return (
                "success",
                "[#%d %s](%s) has been created for branch `%s`"
                % (new_pull.number, new_pull.title, new_pull.html_url, branch_name,),
            )

        return (
            "failure",
            "%s to branch `%s` failed" % (self.KIND.capitalize(), branch_name),
        )

    def run(self, pull, sources, missing_conditions):
        branches = self.config["branches"]
        if self.config["regexes"]:
            regexes = list(map(re.compile, self.config["regexes"]))
            branches.extend(
                (
                    branch.name
                    for branch in pull.g_pull.base.repo.get_branches()
                    if any(map(lambda regex: regex.match(branch.name), regexes))
                )
            )

        results = [self._copy(pull, branch_name) for branch_name in branches]

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
    def get_existing_duplicate_pull(cls, pull, branch):
        bp_branch = duplicate_pull.get_destination_branch_name(pull, branch, cls.KIND)
        # NOTE(sileht): Github looks buggy here, head= doesn't work as expected
        pulls = list(
            p
            for p in pull.g_pull.base.repo.get_pulls(
                base=branch.name, sort="created", state="all"
            )
            if p.head.ref == bp_branch
        )
        if pulls:
            return pulls[-1]
