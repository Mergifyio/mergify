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

import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue


BRANCH_PROTECTION_FAQ_URL = (
    "https://doc.mergify.io/faq.html#"
    "mergify-is-unable-to-merge-my-pull-request-due-to-"
    "my-branch-protection-settings"
)


class MergeAction(actions.Action):
    only_once = True

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("strict", default=False): voluptuous.Any(bool, "smart"),
        voluptuous.Required("strict_method", default="merge"): voluptuous.Any(
            "rebase", "merge"
        ),
    }

    def run(self, pull, sources, missing_conditions):
        pull.log.debug("process merge", config=self.config)

        output = helpers.merge_report(pull, self.config["strict"])
        if output:
            if self.config["strict"] == "smart":
                queue.remove_pull(pull)
            return output

        if self.config["strict"] and pull.is_behind:
            return self._sync_with_base_branch(pull)
        else:
            try:
                return self._merge(pull)
            finally:
                if self.config["strict"] == "smart":
                    queue.remove_pull(pull)

    def cancel(self, pull, sources, missing_conditions):
        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merge if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and self._required_statuses_in_progress(
            pull, missing_conditions
        ):
            return helpers.get_wait_for_ci_report(pull)

        if self.config["strict"] == "smart":
            queue.remove_pull(pull)

        return self.cancelled_check_report

    @staticmethod
    def _required_statuses_in_progress(pull, missing_conditions):
        # It's closed, it's not going to change
        if pull.state == "closed":
            return False

        need_look_at_checks = []
        for condition in missing_conditions:
            if condition.attribute_name.startswith("status-"):
                need_look_at_checks.append(condition)
            else:
                # something else does not match anymore
                return False

        if need_look_at_checks:
            checks = list(pull._get_checks())
            if not checks:
                # No checks have been send yet
                return True

            # Take only checks we care about
            checks = [
                s
                for s in checks
                for cond in need_look_at_checks
                if cond(**{cond.attribute_name: s.context})
            ]
            if not checks:
                return True

            for s in checks:
                if s.state in ("pending", None):
                    return True

        return False

    def _sync_with_base_branch(self, pull):
        if not pull.base_is_modifiable:
            return (
                "failure",
                "Pull request can't be updated with latest "
                "base branch changes, owner doesn't allow "
                "modification",
                "",
            )
        elif self.config["strict"] == "smart":
            queue.add_pull(pull, self.config["strict_method"])
            return (
                None,
                "Base branch will be updated soon",
                "The pull request base branch will "
                "be updated soon, and then merged.",
            )
        else:
            return helpers.update_pull_base_branch(pull, self.config["strict_method"])

    def _merge(self, pull):
        if self.config["method"] != "rebase" or pull.data["rebaseable"]:
            method = self.config["method"]
        elif self.config["rebase_fallback"]:
            method = self.config["rebase_fallback"]
        else:
            return (
                "action_required",
                "Automatic rebasing is not possible, manual intervention required",
                "",
            )

        data = pull.get_merge_commit_message() or {}
        data["sha"] = pull.data["head"]["sha"]
        data["merge_method"] = method

        try:
            pull.client.put(f"pulls/{pull.data['number']}/merge", json=data)
        except httpx.HTTPClientSideError as e:  # pragma: no cover
            pull.update()
            if pull.data["merged"]:
                pull.log.info("merged in the meantime")
            else:
                return self._handle_merge_error(e, pull)
        else:
            pull.update()
            pull.log.info("merged")

        return helpers.merge_report(pull, self.config["strict"])

    def _handle_merge_error(self, e, pull):
        if "Head branch was modified" in e.message:
            pull.log.debug(
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
            pull.log.debug(
                "Base branch was modified in the meantime, retrying",
                status=e.status_code,
                error_message=e.message,
            )
            return self._sync_with_base_branch(pull)

        elif e.status_code == 405:
            pull.log.debug(
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
            pull.log.info(
                "merge fail",
                status=e.status_code,
                mergify_message=message,
                error_message=e.message,
            )
            return ("failure", message, f"GitHub error message: `{e.message}`")
