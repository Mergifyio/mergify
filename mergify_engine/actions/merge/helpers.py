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

from mergify_engine import branch_updater
from mergify_engine.actions.merge import queue


def merge_report(ctxt, strict):
    if ctxt.pull["draft"]:
        conclusion = None
        title = "Draft flag needs to be removed"
        summary = ""
    elif ctxt.pull["merged"]:
        if ctxt.pull["merged_by"]["login"] in [
            "mergify[bot]",
            "mergify-test[bot]",
        ]:
            mode = "automatically"
        else:
            mode = "manually"
        conclusion = "success"
        title = "The pull request has been merged %s" % mode
        summary = "The pull request has been merged %s at *%s*" % (
            mode,
            ctxt.pull["merge_commit_sha"],
        )
    elif ctxt.pull["state"] == "closed":
        conclusion = "cancelled"
        title = "The pull request has been closed manually"
        summary = ""

    # NOTE(sileht): Take care of all branch protection state
    elif ctxt.pull["mergeable_state"] == "dirty":
        conclusion = "failure"
        title = "Merge conflict needs to be solved"
        summary = ""
    elif ctxt.pull["mergeable_state"] == "unknown":
        conclusion = "failure"
        title = "Pull request state reported as `unknown` by GitHub"
        summary = ""
    # FIXME(sileht): We disable this check as github wrongly report
    # mergeable_state == blocked sometimes. The workaround is to try to merge
    # it and if that fail we checks for blocking state.
    # elif ctxt.pull["mergeable_state"] == "blocked":
    #     conclusion = "failure"
    #     title = "Branch protection settings are blocking automatic merging"
    #     summary = ""
    elif ctxt.pull["mergeable_state"] == "behind" and not strict:
        # Strict mode has been enabled in branch protection but not in
        # mergify
        conclusion = "failure"
        title = (
            "Branch protection setting 'strict' conflicts with Mergify configuration"
        )
        summary = ""

    # NOTE(sileht): remaining state "behind, clean, unstable, has_hooks
    # are OK for us
    else:
        return

    return conclusion, title, summary


def get_wait_for_ci_report(pull, rule=None, missing_conditions=None):
    summary = "The pull request has been automatically updated to follow its base branch and will be merged soon."

    pulls = queue.get_pulls_from_queue(pull)
    if pulls:
        links = ", ".join((f"#{pull}" for pull in pulls))
        summary += f"\n\nThe following pull requests are queued: {links}"

    if rule and missing_conditions is not None:
        summary += "\n\nThe required statuses are:\n"
        for cond in rule["conditions"]:
            checked = " " if cond in missing_conditions else "X"
            summary += f"\n- [{checked}] `{cond}`"

    return None, "Base branch updates done", summary


def update_pull_base_branch(pull, method):
    try:
        if method == "merge":
            branch_updater.update_with_api(pull)
        else:
            branch_updater.update_with_git(pull, method)
    except branch_updater.BranchUpdateFailure as e:
        # NOTE(sileht): Maybe the PR have been rebased and/or merged manually
        # in the meantime. So double check that to not report a wrong status
        pull.update()
        output = merge_report(pull, True)
        if output:
            return output
        else:
            return ("failure", "Base branch update has failed", e.message)
    else:
        return get_wait_for_ci_report(pull)
