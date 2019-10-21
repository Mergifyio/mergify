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

import daiquiri

import github

import voluptuous

from mergify_engine import actions
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue

LOG = daiquiri.getLogger(__name__)
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

    def run(
        self,
        installation_id,
        installation_token,
        event_type,
        data,
        pull,
        missing_conditions,
    ):
        LOG.debug("process merge", config=self.config, pull=pull)

        output = helpers.merge_report(pull)
        if output:
            return output

        output = helpers.output_for_mergeable_state(pull, self.config["strict"])
        if output:
            return output

        if self.config["strict"] and pull.is_behind():
            return self._sync_with_base_branch(pull, installation_id)
        else:
            return self._merge(pull, installation_id)

    def cancel(
        self,
        installation_id,
        installation_token,
        event_type,
        data,
        pull,
        missing_conditions,
    ):
        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merge if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and self._required_statuses_in_progress(
            pull, missing_conditions
        ):
            return

        if self.config["strict"] == "smart":
            queue.remove_pull(pull)

        return self.cancelled_check_report

    @staticmethod
    def _required_statuses_in_progress(pull, missing_conditions):
        # It's closed, it's not going to change
        if pull.g_pull.state == "closed":
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

    def _sync_with_base_branch(self, pull, installation_id):
        if not pull.base_is_modifiable():
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
            return helpers.update_pull_base_branch(
                pull, installation_id, self.config["strict_method"]
            )

    def _merge(self, pull, installation_id):
        if self.config["strict"] == "smart":
            queue.remove_pull(pull)

        if self.config["method"] != "rebase" or pull.g_pull.raw_data["rebaseable"]:
            method = self.config["method"]
        elif self.config["rebase_fallback"]:
            method = self.config["rebase_fallback"]
        else:
            return (
                "action_required",
                "Automatic rebasing is not possible, manual intervention required",
                "",
            )

        kwargs = pull.get_merge_commit_message() or {}
        try:
            pull.g_pull.merge(sha=pull.g_pull.head.sha, merge_method=method, **kwargs)
        except github.GithubException as e:  # pragma: no cover
            if pull.g_pull.is_merged():
                LOG.info("merged in the meantime", pull=pull)
            else:
                return self._handle_merge_error(e, pull, installation_id)
        else:
            LOG.info("merged", pull=pull)

        pull.g_pull.update()
        return helpers.merge_report(pull)

    def _handle_merge_error(self, e, pull, installation_id):
        if "Base branch was modified" in e.data["message"]:
            # NOTE(sileht): The base branch was modified between pull.is_behind() call and
            # here, usually by something not merged by mergify. So we need sync it again
            # with the base branch.
            LOG.info("Base branch was modified in the meantime, retrying", pull=pull)
            pull.g_pull.update()
            return self._sync_with_base_branch(pull, installation_id)

        elif e.status != 405:
            message = "Mergify fails to merge the pull request"

        elif pull.g_pull.mergeable_state == "blocked":
            return (
                None,
                "Waiting for the Branch Protection to be validated",
                "Branch Protection is enabled and is preventing Mergify "
                "to merge the pull request. Mergify will merge when "
                "branch protection settings validate the pull request.",
            )

        else:
            message = "Repository settings are blocking automatic merging"

        log_method = LOG.error if e.status >= 500 else LOG.info
        log_method(
            "merge fail",
            status=e.status,
            mergify_message=message,
            error_message=e.data["message"],
            pull=pull,
        )

        return ("failure", message, "GitHub error message: `%s`" % e.data["message"])
