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

import github

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
    for review in pull.mergify_engine["reviews"]:
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
    commit = pull.base.repo.get_commit(pull.head.sha)
    status = commit.get_combined_status()
    return status.state


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


def compute_weight_and_status(pull, **extra):
    status_desc = disabled_by_rules(pull, **extra)
    reviews_ok, reviews_ko, reviews_required = pull.mergify_engine["approvals"]
    if status_desc is not None:
        weight = -1
    elif reviews_ko:
        weight = -1
        status_desc = "Change requests need to be dismissed"
    elif len(reviews_ok) < reviews_required:
        weight = -1
        status_desc = (
            "%d/%d approvals required" %
            (len(reviews_ok), reviews_required)
        )
    elif (pull.mergeable_state == "clean"
          and pull.mergify_engine["combined_status"] == "success"):
        # Best PR ever, up2date and CI OK
        weight = 11
        status_desc = "Will be merged soon"
    elif pull.mergeable_state in ["clean", "unstable"]:
        weight = 10
        status_desc = "Will be merged soon"
    elif (pull.mergeable_state == "blocked"
          and pull.mergify_engine["combined_status"] == "pending"):
        # Maybe clean soon, or maybe this is the previous run
        # selected PR that we just rebase
        weight = 10
        status_desc = "Waiting for CI success"
    elif pull.mergeable_state == "behind":
        # Not up2date, but ready to merge, is branch updatable
        if not pull.maintainer_can_modify:
            weight = -1
            status_desc = ("Pull request can't be updated with latest "
                           "base branch changes, owner doesn't allow "
                           "modification")
        elif pull.mergify_engine["combined_status"] == "success":
            weight = 7
            status_desc = ("Pull request will be updated with latest base "
                           "branch changes soon")
        elif pull.mergify_engine["combined_status"] == "pending":
            weight = 5
            status_desc = "Waiting for CI success"
        else:
            weight = -1
            status_desc = "Waiting for CI success"
    else:
        weight = -1
        status_desc = "Waiting for CI sucess"

    if weight >= 0 and pull.milestone is not None:
        weight += 1
    return (weight, status_desc)


def compute_weight(pull, **extra):
    # TODO(sileht): Don't store this twice
    return pull.mergify_engine["weight_and_status"][0]


def compute_status_desc(pull, **extra):
    # TODO(sileht): Don't store this twice
    return pull.mergify_engine["weight_and_status"][1]


# Order matter, some method need result of some other
FULLIFIER = [
    ("reviews", lambda p, **extra: list(p.get_reviews())),
    ("combined_status", compute_combined_status),
    ("approvals", compute_approvals),          # Need reviews
    ("weight_and_status", compute_weight_and_status),  # Need approvals
    ("weight", compute_weight),                # Need weight_and_status
    ("status_desc", compute_status_desc),      # Need weight_and_status
]

CACHE_HOOK_LIST_CONVERT = {
    "reviews": github.PullRequestReview.PullRequestReview,
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
