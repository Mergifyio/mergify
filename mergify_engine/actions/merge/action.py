# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
import itertools
import re

import daiquiri
import voluptuous

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

BRANCH_PROTECTION_FAQ_URL = (
    "https://doc.mergify.io/faq.html#"
    "mergify-is-unable-to-merge-my-pull-request-due-to-"
    "my-branch-protection-settings"
)

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)


def Priority(v):
    try:
        return helpers.PriorityAliases[v].value
    except KeyError:
        return v


class MergeAction(actions.Action):
    only_once = True

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("strict", default=False): voluptuous.Any(
            bool,
            voluptuous.All("smart", voluptuous.Coerce(lambda _: "smart+ordered")),
            voluptuous.All(
                "smart+fastpath", voluptuous.Coerce(lambda _: "smart+fasttrack")
            ),
            "smart+fasttrack",
            "smart+ordered",
        ),
        voluptuous.Required("strict_method", default="merge"): voluptuous.Any(
            "rebase", "merge"
        ),
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
        voluptuous.Required(
            "priority", default=helpers.PriorityAliases.medium.value
        ): voluptuous.All(
            voluptuous.Any("low", "medium", "high", int),
            voluptuous.Coerce(Priority),
            int,
            voluptuous.Range(min=1, max=10000),
        ),
    }

    def run(self, ctxt, rule, missing_conditions):
        ctxt.log.info("process merge", config=self.config)

        q = queue.Queue.from_context(ctxt)

        output = helpers.merge_report(ctxt, self.config["strict"])
        if output:
            q.remove_pull(ctxt.pull["number"])
            return output

        if self._should_be_merged(ctxt):
            try:
                return self._merge(ctxt)
            finally:
                q.remove_pull(ctxt.pull["number"])
        else:
            return self._sync_with_base_branch(ctxt)

    def _should_be_merged(self, ctxt):
        q = queue.Queue.from_context(ctxt)
        if self.config["strict"] in ("smart+fasttrack", "smart+ordered"):
            if self.config["strict"] == "smart+ordered":
                return not ctxt.is_behind and q.is_first_pull(ctxt.pull["number"])
            elif self.config["strict"] == "smart+fasttrack":
                return not ctxt.is_behind
            else:
                raise RuntimeError("Unexpected strict_smart_behavior")
        elif self.config["strict"]:
            return not ctxt.is_behind
        else:
            return True

    def cancel(self, ctxt, rule, missing_conditions):
        q = queue.Queue.from_context(ctxt)
        if ctxt.pull["state"] == "closed":
            output = helpers.merge_report(ctxt, self.config["strict"])
            if output:
                q.remove_pull(ctxt.pull["number"])
                return output

        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merge if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and self._required_statuses_in_progress(
            ctxt, missing_conditions
        ):
            return helpers.get_strict_status(
                ctxt, rule, missing_conditions, need_update=ctxt.is_behind
            )

        q.remove_pull(ctxt.pull["number"])

        return self.cancelled_check_report

    @staticmethod
    def _required_statuses_in_progress(ctxt, missing_conditions):
        # It's closed, it's not going to change
        if ctxt.pull["state"] == "closed":
            return False

        need_look_at_checks = []
        for condition in missing_conditions:
            if condition.attribute_name.startswith("status-"):
                # TODO(sileht): Just return True here, no need to checks
                # checks anymore, this method is no more use by merge queue
                need_look_at_checks.append(condition)
            else:
                # something else does not match anymore
                return False

        if need_look_at_checks:
            if not ctxt.checks:
                return True

            states = [
                state
                for name, state in ctxt.checks.items()
                for cond in need_look_at_checks
                if cond(**{cond.attribute_name: name})
            ]
            if not states:
                return True

            for state in states:
                if state in ("pending", None):
                    return True

        return False

    def _sync_with_base_branch(self, ctxt):
        # If PR from a public fork but cannot be edited
        if (
            ctxt.pull_from_fork
            and not ctxt.pull["base"]["repo"]["private"]
            and not ctxt.pull["maintainer_can_modify"]
        ):
            return (
                "failure",
                "Pull request can't be updated with latest base branch changes",
                "Mergify needs the permission to update the base branch of the pull request.\n"
                f"{ctxt.pull['base']['repo']['owner']['login']} needs to "
                "[authorize modification on its base branch]"
                "(https://help.github.com/articles/allowing-changes-to-a-pull-request-branch-created-from-a-fork/).",
            )
        # If PR from a private fork but cannot be edited:
        # NOTE(jd): GitHub removed the ability to configure `maintainer_can_modify` on private fork we which make strict mode broken
        elif (
            ctxt.pull_from_fork
            and ctxt.pull["base"]["repo"]["private"]
            and not ctxt.pull["maintainer_can_modify"]
        ):
            return (
                "failure",
                "Pull request can't be updated with latest base branch changes",
                "Mergify needs the permission to update the base branch of the pull request.\n"
                "GitHub does not allow a GitHub App to modify base branch for a private fork.\n"
                "You cannot use strict mode with a pull request from a private fork.",
            )
        elif self.config["strict"] in ("smart+fasttrack", "smart+ordered"):
            queue.Queue.from_context(ctxt).add_pull(ctxt, self.config)
            return helpers.get_strict_status(ctxt, need_update=ctxt.is_behind)
        else:
            return helpers.update_pull_base_branch(ctxt, self.config["strict_method"])

    @staticmethod
    def _get_commit_message(pull_request, mode="default"):
        if mode == "title+body":
            # Include PR number to mimic default GitHub format
            return f"{pull_request.title} (#{pull_request.number})", pull_request.body

        if not pull_request.body:
            return

        found = False
        message_lines = []

        for line in pull_request.body.split("\n"):
            if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                found = True
            elif found and MARKDOWN_TITLE_RE.match(line):
                break
            elif found:
                message_lines.append(line)

        # Remove the first empty lines
        message_lines = list(
            itertools.dropwhile(lambda x: not x.strip(), message_lines)
        )

        if found and message_lines:
            title = message_lines.pop(0)

            # Remove the empty lines between title and message body
            message_lines = list(
                itertools.dropwhile(lambda x: not x.strip(), message_lines)
            )

            return (
                pull_request.render_template(title.strip()),
                pull_request.render_template(
                    "\n".join(line.strip() for line in message_lines)
                ),
            )

    def _merge(self, ctxt):
        if self.config["method"] != "rebase" or ctxt.pull["rebaseable"]:
            method = self.config["method"]
        elif self.config["rebase_fallback"]:
            method = self.config["rebase_fallback"]
        else:
            return (
                "action_required",
                "Automatic rebasing is not possible, manual intervention required",
                "",
            )

        data = {}

        try:
            commit_title_and_message = self._get_commit_message(
                ctxt.pull_request, self.config["commit_message"],
            )
        except context.RenderTemplateFailure as rmf:
            return (
                "action_required",
                "Invalid commit message",
                str(rmf),
            )

        if commit_title_and_message is not None:
            title, message = commit_title_and_message
            if title:
                data["commit_title"] = title
            if message:
                data["commit_message"] = message

        data["sha"] = ctxt.pull["head"]["sha"]
        data["merge_method"] = method

        try:
            ctxt.client.put(f"pulls/{ctxt.pull['number']}/merge", json=data)
        except http.HTTPClientSideError as e:  # pragma: no cover
            ctxt.update()
            if ctxt.pull["merged"]:
                ctxt.log.info("merged in the meantime")
            else:
                return self._handle_merge_error(e, ctxt)
        else:
            ctxt.update()
            ctxt.log.info("merged")

        return helpers.merge_report(ctxt, self.config["strict"])

    def _handle_merge_error(self, e, ctxt):
        if "Head branch was modified" in e.message:
            ctxt.log.info(
                "Head branch was modified in the meantime",
                status=e.status_code,
                error_message=e.message,
            )
            return (
                "cancelled",
                "Head branch was modified in the meantime",
                "The head branch was modified, the merge action have been cancelled.",
            )
        elif "Base branch was modified" in e.message:
            # NOTE(sileht): The base branch was modified between pull.is_behind call and
            # here, usually by something not merged by mergify. So we need sync it again
            # with the base branch.
            ctxt.log.info(
                "Base branch was modified in the meantime, retrying",
                status=e.status_code,
                error_message=e.message,
            )
            return self._sync_with_base_branch(ctxt)

        elif e.status_code == 405:
            ctxt.log.info(
                "Waiting for the Branch Protection to be validated",
                status=e.status_code,
                error_message=e.message,
            )
            return (
                None,
                "Waiting for the Branch Protection to be validated",
                "Branch Protection is enabled and is preventing Mergify "
                "to merge the pull request. Mergify will merge when "
                "branch protection settings validate the pull request. "
                f"(detail: {e.message})",
            )
        else:
            message = "Mergify failed to merge the pull request"
            ctxt.log.info(
                "merge fail",
                status=e.status_code,
                mergify_message=message,
                error_message=e.message,
            )
            return ("failure", message, f"GitHub error message: `{e.message}`")
