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

import voluptuous

from mergify_engine import actions
from mergify_engine import branch_updater

LOG = daiquiri.getLogger(__name__)


class MergeAction(actions.Action):
    cancel_in_progress = False

    validator = {
        voluptuous.Required("method", default="merge"):
        voluptuous.Any("rebase", "merge", "squash"),
        voluptuous.Required("rebase_fallback", default="merge"):
        voluptuous.Any("merge", "squash", None),
        voluptuous.Required("strict", default=False): bool,
    }

    def __call__(self, installation_id, installation_token, subscription,
                 event_type, data, pull):

        # TODO(sileht): set correct error message if mergeable_state !=
        # unstable/clean about conflict with the branch protection
        # TODO(sileht): some error should be report in Github UI
        # TODO(sileht): Don't blindly update all PRs, but just one by one.

        if self.config["strict"] and pull.is_behind():
            updated = branch_updater.update(pull, subscription["token"])
            if not updated:
                raise Exception("branch update of %s have failed" % pull)

            # NOTE(sileht): We update g_pull to have the new head.sha, so
            # future created checks will be post on the new sha. Otherwise
            # the checks will be lost the GitHub UI on the old sha.
            pull.wait_for_sha_change()

            return (None, "The pull request has been automatically "
                    "updated to follow its base branch and will be merged "
                    "soon")

        else:

            merged = pull.merge(self.config["method"],
                                self.config["rebase_fallback"])
            if not merged:
                raise Exception("merge of %s have failed" % pull)
            LOG.info("merged", pull_request=pull)
            pull.g_pull.update()
            return ("success", "The pull request have been automatically "
                    "merged at *%s*" % pull.g_pull.merge_commit_sha)
