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
from mergify_engine import branch_updater

LOG = daiquiri.getLogger(__name__)


class MergeAction(actions.Action):
    validator = {
        voluptuous.Required("method", default="merge"):
        voluptuous.Any("rebase", "merge", "squash"),
        voluptuous.Required("rebase_fallback", default="merge"):
        voluptuous.Any("merge", "squash", None),
        voluptuous.Required("strict", default=False): bool,
    }

    def run(self, installation_id, installation_token, subscription,
            event_type, data, pull):
        pull.log.debug("process merge", config=self.config)

        # NOTE(sileht): Take care of all branch protection state
        if pull.g_pull.mergeable_state == "dirty":
            return None, "Merge conflict needs to be solved", ""
        elif pull.g_pull.mergeable_state == "unknown":
            return ("failure", "Pull request state reported as `unknown` by "
                    "GitHub", "")
        elif pull.g_pull.mergeable_state == "blocked":
            return ("failure", "Branch protection settings are blocking "
                    "automatic merging", "")
        elif (pull.g_pull.mergeable_state == "behind" and
              not self.config["strict"]):
            # Strict mode has been enabled in branch protection but not in
            # mergify
            return ("failure", "Branch protection setting 'strict' conflicts "
                    "with Mergify configuration", "")
        # NOTE(sileht): remaining state "behind, clean, unstable, has_hooks"
        # are OK for us

        if self.config["strict"] and pull.is_behind():
            # TODO(sileht): strict: Don't blindly update all PRs, but just one
            # by one.

            updated = branch_updater.update(pull, subscription["token"])
            if updated:
                # NOTE(sileht): We update g_pull to have the new head.sha, so
                # future created checks will be post on the new sha. Otherwise
                # the checks will be lost the GitHub UI on the old sha.
                pull.wait_for_sha_change()

                return (None, "Base branch updates done",
                        "The pull request has been automatically "
                        "updated to follow its base branch and will be "
                        "merged soon")
            else:  # pragma: no cover
                return ("failure", "Base branch update has failed", "")

        else:
            if (self.config["method"] != "rebase" or
                    pull.g_pull.raw_data['rebaseable']):
                return self._merge(pull, self.config["method"])
            elif self.config["rebase_fallback"]:
                return self._merge(pull, self.config["rebase_fallback"])
            else:
                return ("action_required", "Automatic rebasing is not "
                        "possible, manual intervention required", "")

    @staticmethod
    def _merge(pull, method):
        try:
            pull.g_pull.merge(sha=pull.g_pull.head.sha,
                              merge_method=method)
        except github.GithubException as e:   # pragma: no cover
            if pull.g_pull.is_merged():
                pull.log.info("merged in the meantime")

            elif e.status == 405:
                pull.log.error("merge fail", error=e.data["message"])
                return ("failure",
                        "Repository settings are blocking automatic merging",
                        e.data["message"])

            elif 400 <= e.status < 500:
                pull.log.error("merge fail", error=e.data["message"])
                return ("failure",
                        "Mergify fails to merge the pull request",
                        e.data["message"])
            else:
                raise
        else:
            pull.log.info("merged")
        pull.g_pull.update()
        return ("success",
                "The pull request has been automatically merged",
                "The pull request has been automatically "
                "merged at *%s*" % pull.g_pull.merge_commit_sha)
