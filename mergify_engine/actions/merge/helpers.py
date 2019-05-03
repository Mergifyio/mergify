# -*- encoding: utf-8 -*-
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

from mergify_engine import branch_updater
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


def merge_report(pull):
    if pull.g_pull.merged:
        if pull.g_pull.merged_by.login == 'mergify[bot]':
            mode = "automatically"
        else:
            mode = "manually"
        conclusion = "success"
        title = "The pull request has been merged %s" % mode
        summary = ("The pull request has been merged %s at *%s*" %
                   (mode, pull.g_pull.merge_commit_sha))
    elif pull.g_pull.state == "closed":
        conclusion = "cancelled"
        title = "The pull request has been closed manually"
        summary = ""
    else:
        return

    return conclusion, title, summary


def output_for_mergeable_state(pull, strict):
    # NOTE(sileht): Take care of all branch protection state
    if pull.g_pull.mergeable_state == "dirty":
        return None, "Merge conflict needs to be solved", ""
    elif pull.g_pull.mergeable_state == "unknown":
        return ("failure", "Pull request state reported as `unknown` by "
                "GitHub", "")
    # FIXME(sileht): We disable this check as github wrongly report
    # mergeable_state == blocked sometimes. The workaround is to try to merge
    # it and if that fail we checks for blocking state.
    # elif pull.g_pull.mergeable_state == "blocked":
    #     return ("failure", "Branch protection settings are blocking "
    #            "automatic merging", "")
    elif (pull.g_pull.mergeable_state == "behind" and not strict):
        # Strict mode has been enabled in branch protection but not in
        # mergify
        return ("failure", "Branch protection setting 'strict' conflicts "
                "with Mergify configuration", "")
        # NOTE(sileht): remaining state "behind, clean, unstable, has_hooks"
        # are OK for us


def update_pull_base_branch(pull, installation_id, method):
    try:
        updated = branch_updater.update(pull, installation_id, method)
    except branch_updater.BranchUpdateFailure as e:
        # NOTE(sileht): Maybe the PR have been rebased and/or merged manually
        # in the meantime. So double check that to not report a wrong status
        pull.g_pull.update()
        output = merge_report(pull)
        if output:
            return output
        else:
            return ("failure", "Base branch update has failed", e.message)
    else:
        redis = utils.get_redis_for_cache()
        # NOTE(sileht): We store this for dismissal action
        redis.setex("branch-update-%s" % updated, 60 * 60, updated)

        # NOTE(sileht): We update g_pull to have the new head.sha,
        # so future created checks will be posted on the new sha.
        # Otherwise the checks will be lost the GitHub UI on the
        # old sha.
        pull.wait_for_sha_change()
        return (None, "Base branch updates done",
                "The pull request has been automatically "
                "updated to follow its base branch and will be "
                "merged soon")
