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

LOG = logging.getLogger(__name__)

UNUSABLE_STATES = ["unknown", None]


def ensure_mergable_state(pull):
    if pull.is_merged() or pull.mergeable_state not in UNUSABLE_STATES:
        return pull

    # Github is currently processing this PR, we wait the completion
    for i in range(0, 5):
        LOG.info("%s, refreshing...", pull.pretty())
        pull.update()
        if pull.is_merged() or pull.mergeable_state not in UNUSABLE_STATES:
            break
        time.sleep(0.42)  # you known, this one always work

    return pull


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

    try:
        required = extra["branch_rule"]["protection"][
            "required_pull_request_reviews"]["required_approving_review_count"]
    except KeyError:
        return [], [], 0

    return ([users_info[u] for u in reviews_ok],
            [users_info[u] for u in reviews_ko],
            required)


def compute_combined_status(pull, **extra):
    try:
        contexts = extra["branch_rule"]["protection"][
            "required_status_checks"]["contexts"]
    except KeyError:
        return "success"

    commit = pull.base.repo.get_commit(pull.head.sha)
    status = commit.get_combined_status()

    combined_status = "success"
    for check in status.statuses:
        if check.context in contexts:
            if check.state in ["error", "failure"]:
                return "failure"
            elif check.state == "pending":
                combined_status = "pending"

    return combined_status


def disabled_by_rules(pull, **extra):
    labels = [l.name for l in pull.labels]
    if extra["branch_rule"]["disabling_label"] in labels:
        return "Disabled with label"

    pull_files = [f.filename for f in pull.get_files()]
    for w in extra["branch_rule"]["disabling_files"]:
        filtered = fnmatch.filter(pull_files, w)
        if filtered:
            return ("Disabled, %s is modified in this pull request"
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


# Use enum.Enum and make is serializable ?
class MergifyState(object):
    NOT_READY = 0
    NEED_BRANCH_UPDATE = 10
    ALMOST_READY = 20
    READY = 30


def compute_status(pull, **extra):
    disabled = disabled_by_rules(pull, **extra)
    reviews_ok, reviews_ko, reviews_required = pull.mergify_engine["approvals"]

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
    elif (pull.mergeable_state == "blocked"
          and pull.mergify_engine["combined_status"] == "pending"):
        # Maybe clean soon, or maybe this is the previous run selected PR that
        # we just rebase, or maybe not. But we set the mergify_state to
        # ALMOST_READY to ensure we do not rebase multiple pull request in //
        mergify_state = MergifyState.ALMOST_READY
    elif pull.mergeable_state == "behind":
        # Not up2date, but ready to merge, is branch updatable
        if not pull.base_is_modifiable:
            github_state = "failure"
            github_desc = ("Pull request can't be updated with latest "
                           "base branch changes, owner doesn't allow "
                           "modification")
        elif pull.mergify_engine["combined_status"] == "success":
            mergify_state = MergifyState.NEED_BRANCH_UPDATE
            github_desc = ("Pull request will be updated with latest base "
                           "branch changes soon")

    return {"mergify_state": mergify_state,
            "github_description": github_desc,
            "github_state": github_state}


# Order matter, some method need result of some other
FULLIFIER = [
    ("combined_status", compute_combined_status),
    ("approvals", compute_approvals),   # Need reviews
    ("status", compute_status),         # Need approvals and combined_status
]

CACHE_HOOK_LIST_CONVERT = {
}


def jsonify(pull):
    raw = copy.copy(pull.raw_data)
    for key, method in FULLIFIER:
        value = pull.mergify_engine[key]
        if key in CACHE_HOOK_LIST_CONVERT:
            try:
                value = [item.raw_data for item in value]
            except AttributeError:
                LOG.exception("%s, fail to cache %s: %s",
                              pull.pretty(), key, value)

        raw["mergify_engine_%s" % key] = value
    return raw


def fullify(pull, cache=None, **extra):
    LOG.debug("%s, fullifing...", pull.pretty())
    if not hasattr(pull, "mergify_engine"):
        pull.mergify_engine = {}

    pull = ensure_mergable_state(pull)

    for key, method in FULLIFIER:
        if key not in pull.mergify_engine:
            if cache and "mergify_engine_%s" % key in cache:
                value = cache["mergify_engine_%s" % key]
                klass = CACHE_HOOK_LIST_CONVERT.get(key)
                if klass:
                    value = [klass(pull.base.repo._requester, {}, item,
                                   completed=True) for item in value]
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
