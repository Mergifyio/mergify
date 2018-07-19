# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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

import copy
import fnmatch
import logging
import time

from github import Consts

from mergify_engine import exceptions

LOG = logging.getLogger(__name__)

UNUSABLE_STATES = ["unknown", None]


def ensure_mergable_state(pull, force=False):
    if pull.merged:
        return
    if not force and pull.mergeable_state not in UNUSABLE_STATES:
        return

    # Github is currently processing this PR, we wait the completion
    # TODO(sileht): We should be able to do better that retry 15x
    for i in range(0, 5):
        LOG.info("%s, refreshing...", pull.pretty())

        # FIXME(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change...
        pull._headers.pop(Consts.RES_ETAG, None)
        pull._headers.pop(Consts.RES_LAST_MODIFIED, None)
        pull.update()
        if pull.merged or pull.mergeable_state not in UNUSABLE_STATES:
            return
        time.sleep(0.42)  # you known, this one always work


def compute_approvals(pull, **extra):
    """Compute approvals.

    :param pull: The pull request.
    :param extra: Extra stuff
    :return: A tuple (users_with_review_ok, users_with_review_ko,
                      number_of_review_required)
    """
    users_info = {}
    reviews_ok = set()
    reviews_ko = set()
    for review in pull.get_reviews():
        if review.user.id not in extra["collaborators"]:
            continue

        users_info[review.user.login] = review.user.raw_data
        if review.state == 'APPROVED':
            reviews_ok.add(review.user.login)
            if review.user.login in reviews_ko:
                reviews_ko.remove(review.user.login)

        elif review.state in ["DISMISSED", "CHANGES_REQUESTED"]:
            if review.user.login in reviews_ok:
                reviews_ok.remove(review.user.login)
            if review.user.login in reviews_ko:
                reviews_ko.remove(review.user.login)
            if review.state == "CHANGES_REQUESTED":
                reviews_ko.add(review.user.login)
        elif review.state == 'COMMENTED':
            pass
        else:
            LOG.error("%s FIXME review state unhandled: %s",
                      pull.pretty(), review.state)

    protection = extra["branch_rule"]["protection"]
    if not protection["required_pull_request_reviews"]:
        return [], [], 0

    required = protection["required_pull_request_reviews"
                          ]["required_approving_review_count"]

    return ([users_info[u] for u in reviews_ok],
            [users_info[u] for u in reviews_ko],
            required)


def find_required_context(contexts, status_check):
    for c in contexts:
        if status_check.context.startswith(c):
            return c


# Use enum.Enum and make is serializable ?
class StatusState(object):
    FAILURE = 0
    SUCCESS = 1
    PENDING = 2


def compute_required_statuses(pull, **extra):
    # return True is CIs succeed, False is their fail, None
    # is we don't known yet.
    # FIXME(sileht): I don't use a Enum yet to not
    protection = extra["branch_rule"]["protection"]
    if not protection["required_status_checks"]:
        return StatusState.SUCCESS

    commit = pull.base.repo.get_commit(pull.head.sha)
    status = commit.get_combined_status()

    contexts = set(protection["required_status_checks"]["contexts"])
    seen_contexts = set()

    for status_check in status.statuses:
        required_context = find_required_context(contexts, status_check)
        if required_context:
            seen_contexts.add(required_context)
            if status_check.state in ["pending", None]:
                return StatusState.PENDING
            elif status_check.state != "success":
                return StatusState.FAILURE

    if contexts - seen_contexts:
        return StatusState.PENDING
    else:
        return StatusState.SUCCESS


def disabled_by_rules(pull, **extra):
    labels = [l.name for l in pull.labels]

    enabling_label = extra["branch_rule"]["enabling_label"]
    if enabling_label is not None and enabling_label not in labels:
        return "Disabled — enabling label missing"

    if extra["branch_rule"]["disabling_label"] in labels:
        return "Disabled — disabling label present"

    pull_files = [f.filename for f in pull.get_files()]
    for w in extra["branch_rule"]["disabling_files"]:
        filtered = fnmatch.filter(pull_files, w)
        if filtered:
            return ("Disabled — %s is modified"
                    % filtered[0])
    return None

# NOTE(sileht): Github mergeable_state is undocumented, here my finding by
# testing and and some info from other project:
#
# unknown: not yet computed by Github
# dirty: pull request conflict with the base branch
# behind: head branch is behind the base branch (only if strict: True)
# unstable: branch up2date (if strict: True) and not required status
#           checks are failure or pending
# clean: branch up2date (if strict: True) and all status check OK
#
# https://platform.github.community/t/documentation-about-mergeable-state/4259
# https://github.com/octokit/octokit.net/issues/1763
# https://developer.github.com/v4/enum/mergestatestatus/


# Use enum.Enum and make is serializable ?
class MergifyState(object):
    NOT_READY = 0
    NEED_BRANCH_UPDATE = 10
    ALMOST_READY = 20
    READY = 30


def compute_status(pull, **extra):
    disabled = disabled_by_rules(pull, **extra)

    # TODO(sileht): remove me
    if len(pull.mergify_engine["approvals"]) == 4:
        reviews_ok, reviews_ko, reviews_required, _ = \
            pull.mergify_engine["approvals"]
    else:
        reviews_ok, reviews_ko, reviews_required = \
            pull.mergify_engine["approvals"]

    mergify_state = MergifyState.NOT_READY
    github_state = "pending"
    github_desc = "Waiting for status checks success"

    if disabled:
        github_state = "failure"
        github_desc = disabled
    elif reviews_ko:
        github_desc = "Change requests need to be dismissed"
    elif len(reviews_ok) < reviews_required:
        mergify_state = MergifyState.NOT_READY
        github_state = "pending"
        github_desc = (
            "%d/%d approvals required" %
            (len(reviews_ok), reviews_required)
        )
    elif pull.mergeable_state in ["clean", "unstable"]:
        mergify_state = MergifyState.READY
        github_state = "success"
        github_desc = "Will be merged soon"
    elif pull.mergeable_state == "blocked":
        if (pull.mergify_engine["required_statuses"] ==
                StatusState.SUCCESS):

            # NOTE(sileht): CI passed but we are blocked, since we are unable
            # to known if that code owner is required or not, we guess it is.
            # If it's not that a bug in Mergify or Github
            protection = extra["branch_rule"]["protection"]
            required_reviews = protection["required_pull_request_reviews"]
            if required_reviews and required_reviews[
                    "require_code_owner_reviews"]:
                github_desc = "Waiting for code owner review"
            else:
                # NOTE(sileht): The mergeable_state is obviously wrong, we
                # can't have required reviewers and combined_status to success.
                # Sometimes Github is laggy to compute mergeable_state, so
                # refreshing the the pull. Or maybe this is a mergify bug :),
                # so retry only 3 times

                LOG.error("%s: the status is unexpected, requeue this event.",
                          pull.pretty())
                commit = pull.base.repo.get_commit(pull.head.sha)
                status = commit.get_combined_status()
                LOG.warning("reviews: %s", [r.raw_data for r in
                                            list(pull.get_reviews())])
                LOG.warning("status checks: %s", status.raw_data["statuses"])
                LOG.warning("pull content: %s", pull.raw_data)

                # NOTE(sileht): We have reviewers and the CI is OK, so this PR
                # can be merged. But Github tell us it's blocked. As workaround
                # we refresh our status_check, so Github should recompute the
                # mergify_state and allow us to push the merge btn.
                pull.mergify_engine_github_post_check_status(
                    extra["redis"], extra["installation_id"],
                    "success", "Will be merged soon")
                time.sleep(1)
                raise exceptions.RetryJob(3)

        elif (pull.mergify_engine["required_statuses"] ==
              StatusState.PENDING):
            # Maybe clean soon, or maybe this is the previous run selected PR
            # that we just rebase, or maybe not. But we set the mergify_state
            # to ALMOST_READY to ensure we do not rebase multiple pull request
            # in //
            mergify_state = MergifyState.ALMOST_READY

    elif pull.mergeable_state == "behind":
        # Not up2date, but ready to merge, is branch updatable
        if not pull.base_is_modifiable:
            github_state = "failure"
            github_desc = ("Pull request can't be updated with latest "
                           "base branch changes, owner doesn't allow "
                           "modification")
        elif (pull.mergify_engine["required_statuses"] ==
              StatusState.SUCCESS):
            mergify_state = MergifyState.NEED_BRANCH_UPDATE
            github_desc = ("Pull request will be updated with latest base "
                           "branch changes soon")

    return {"mergify_state": mergify_state,
            "github_description": github_desc,
            "github_state": github_state}


# Order matter, some method need result of some other
FULLIFIER = [
    ("required_statuses", compute_required_statuses),
    ("approvals", compute_approvals),   # Need reviews
    ("status", compute_status),         # Need approvals and combined_status
]


def jsonify(pull):
    raw = copy.copy(pull.raw_data)
    for key, method in FULLIFIER:
        value = pull.mergify_engine[key]
        raw["mergify_engine_%s" % key] = value
    return raw


def fullify(pull, cache=None, force=False, **extra):
    LOG.debug("%s, fullifing...", pull.pretty())
    if not hasattr(pull, "mergify_engine"):
        pull.mergify_engine = {}

    ensure_mergable_state(pull, force)

    for key, method in FULLIFIER:
        if key not in pull.mergify_engine or force:
            if cache and "mergify_engine_%s" % key in cache:
                value = cache["mergify_engine_%s" % key]
            elif key == "raw_data":
                value = method(pull, **extra)
            else:
                start = time.time()
                LOG.info("%s, compute %s" % (pull.pretty(), key))
                value = method(pull, **extra)
                LOG.debug("%s, %s computed in %s sec" % (
                    pull.pretty(), key, time.time() - start))

            pull.mergify_engine[key] = value

    LOG.debug("%s, fullified", pull.pretty())
    return pull
