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

import copy
import fnmatch
import functools
import logging

import attr
import github
from github import Consts
import tenacity

from mergify_engine import config

LOG = logging.getLogger(__name__)

# NOTE(sileht): Github mergeable_state is undocumented, here my finding by
# testing and and some info from other project:
#
# unknown: not yet computed by Github
# dirty: pull request conflict with the base branch
# behind: head branch is behind the base branch (only if strict: True)
# unstable: branch up2date (if strict: True) and not required status
#           checks are failure or pending
# clean: branch up2date (if strict: True) and all status check OK
# has_hooks: Mergeable with passing commit status and pre-recieve hooks.
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


# Use enum.Enum and make is serializable ?
class StatusState(object):
    FAILURE = 0
    SUCCESS = 1
    PENDING = 2


@functools.total_ordering
@attr.s(cmp=False)
class MergifyPull(object):
    # NOTE(sileht): Use from_cache/from_event not the constructor directly
    g_pull = attr.ib()
    _complete = attr.ib(init=False, default=False)

    # Cached attributes
    _approvals = attr.ib(init=False, default=None)
    _required_statuses = attr.ib(init=False, default=None)
    _mergify_state = attr.ib(init=False, default=None)
    _github_state = attr.ib(init=False, default=None)
    _github_description = attr.ib(init=False, default=None)

    def __attrs_post_init__(self):
        self._ensure_mergable_state()

    def _ensure_complete(self):
        if not self._complete:
            raise RuntimeError("%s: used an incomplete MergifyPull")

    @property
    def status(self):
        # TODO(sileht): Should be removed at some point. When the cache
        # will not have mergify_engine_status key anymore
        return {"mergify_state": self.mergify_state,
                "github_state": self.github_state,
                "github_description": self.github_description}

    @property
    def required_statuses(self):
        self._ensure_complete()
        return self._required_statuses

    @property
    def approvals(self):
        self._ensure_complete()
        return self._approvals

    @property
    def mergify_state(self):
        self._ensure_complete()
        return self._mergify_state

    @property
    def github_state(self):
        self._ensure_complete()
        return self._github_state

    @property
    def github_description(self):
        self._ensure_complete()
        return self._github_description

    def complete(self, cache, **context):
        need_to_be_saved = False
        if "mergify_engine_approvals" in cache:
            self._approvals = cache["mergify_engine_approvals"]
        else:
            need_to_be_saved = True
            self._approvals = self._compute_approvals(**context)

        if "mergify_engine_required_statuses" in cache:
            self._required_statuses = cache[
                "mergify_engine_required_statuses"]
        else:
            need_to_be_saved = True
            self._required_statuses = self._compute_required_statuses(
                **context)

        if "mergify_engine_status" in cache:
            s = cache["mergify_engine_status"]
            self._mergify_state = s["mergify_state"]
            self._github_state = s["github_state"]
            self._github_description = s["github_description"]
        else:
            need_to_be_saved = True
            (self._mergify_state, self._github_state,
             self._github_description) = self._compute_status(**context)

        self._complete = True
        return need_to_be_saved

    def refresh(self, **context):
        self._ensure_mergable_state(force=True)
        return self.complete({}, **context)

    def jsonify(self):
        raw = copy.copy(self.g_pull.raw_data)
        raw["mergify_engine_approvals"] = self._approvals
        raw["mergify_engine_required_statuses"] = self._required_statuses
        raw["mergify_engine_status"] = {
            "mergify_state": self._mergify_state,
            "github_description": self._github_description,
            "github_state": self._github_state
        }
        return raw

    def _compute_approvals(self, **context):
        """Compute approvals.

        :return: A tuple (users_with_review_ok, users_with_review_ko,
                          number_of_review_required)
        """

        users_info = {}
        reviews_ok = set()
        reviews_ko = set()
        for review in self.g_pull.get_reviews():
            if review.user.id not in context["collaborators"]:
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
                          self, review.state)

        protection = context["branch_rule"]["protection"]
        if protection["required_pull_request_reviews"]:
            required = protection["required_pull_request_reviews"
                                  ]["required_approving_review_count"]

            return ([users_info[u] for u in reviews_ok],
                    [users_info[u] for u in reviews_ko],
                    required)
        else:
            return [], [], 0

    @staticmethod
    def _find_required_context(contexts, status_check):
        for c in contexts:
            if status_check.context.startswith(c):
                return c

    def _compute_required_statuses(self, **context):
        # return True is CIs succeed, False is their fail, None
        # is we don't known yet.
        # FIXME(sileht): I don't use a Enum yet to not
        protection = context["branch_rule"]["protection"]
        if not protection["required_status_checks"]:
            return StatusState.SUCCESS

        commit = self.g_pull.base.repo.get_commit(self.g_pull.head.sha)
        status = commit.get_combined_status()

        contexts = set(protection["required_status_checks"]["contexts"])
        seen_contexts = set()

        for status_check in status.statuses:
            required_context = self._find_required_context(contexts,
                                                           status_check)
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

    def _disabled_by_rules(self, **context):
        labels = [l.name for l in self.g_pull.labels]

        enabling_label = context["branch_rule"]["enabling_label"]
        if enabling_label is not None and enabling_label not in labels:
            return "Disabled — enabling label missing"

        if context["branch_rule"]["disabling_label"] in labels:
            return "Disabled — disabling label present"

        self.g_pull_files = [f.filename for f in self.g_pull.get_files()]
        for w in context["branch_rule"]["disabling_files"]:
            filtered = fnmatch.filter(self.g_pull_files, w)
            if filtered:
                return ("Disabled — %s is modified"
                        % filtered[0])
        return None

    def _compute_status(self, **context):
        disabled = self._disabled_by_rules(**context)

        reviews_ok, reviews_ko, reviews_required = self._approvals

        mergify_state = MergifyState.NOT_READY
        github_state = "pending"
        github_desc = None

        if disabled:
            github_state = "failure"
            github_desc = disabled

        elif reviews_ko:
            github_desc = "Change requests need to be dismissed"

        elif len(reviews_ok) < reviews_required:
            github_desc = (
                "%d/%d approvals required" %
                (len(reviews_ok), reviews_required)
            )

        elif self.g_pull.mergeable_state in ["clean", "unstable", "has_hooks"]:
            mergify_state = MergifyState.READY
            github_state = "success"
            github_desc = "Will be merged soon"

        elif self.g_pull.mergeable_state == "blocked":
            if self._required_statuses == StatusState.SUCCESS:
                # FIXME(sileht) We are blocked but reviews are OK and CI passes
                # So It's a Github bug or Github block the PR about something
                # we don't yet support.

                # We don't fully support require_code_owner_reviews, try so do
                # some guessing.
                protection = context["branch_rule"]["protection"]
                if (protection["required_pull_request_reviews"]
                        and protection["required_pull_request_reviews"
                                       ]["require_code_owner_reviews"]
                        and (self.g_pull._rawData['requested_teams'] or
                             self.g_pull._rawData['requested_reviewers'])):
                    github_desc = "Waiting for code owner review"

                else:
                    # NOTE(sileht): assume it's the Github bug and the PR is
                    # ready, if it's not the merge button will just fail.
                    LOG.error("%s: the mergeable_state is unexpected, trying "
                              "to merge the self.g_pull request." % self)
                    mergify_state = MergifyState.READY
                    github_state = "success"
                    github_desc = "Will be merged soon"

            elif self._required_statuses == StatusState.PENDING:
                # Maybe clean soon, or maybe this is the previous run selected
                # PR that we just rebase, or maybe not. But we set the
                # mergify_state to ALMOST_READY to ensure we do not rebase
                # multiple self.g_pull request in //
                mergify_state = MergifyState.ALMOST_READY
                github_desc = "Waiting for status checks success"
            else:
                github_desc = "Waiting for status checks success"

        elif self.g_pull.mergeable_state == "behind":
            # Not up2date, but ready to merge, is branch updatable
            if not self.base_is_modifiable():
                github_state = "failure"
                github_desc = ("Pull request can't be updated with latest "
                               "base branch changes, owner doesn't allow "
                               "modification")
            elif self._required_statuses == StatusState.SUCCESS:
                mergify_state = MergifyState.NEED_BRANCH_UPDATE
                github_desc = ("Pull request will be updated with latest base "
                               "branch changes soon")
            else:
                github_desc = "Waiting for status checks success"

        elif self.g_pull.mergeable_state == "dirty":
            github_desc = "Merge conflict need to be solved"

        elif self.g_pull.mergeable_state == "unknown":
            # Should not really occur, but who known
            github_desc = "Pull request state reported unknown by Github"
        else:
            raise RuntimeError("%s: Unexpected mergify_state" % self)

        if github_desc is None:
            # Seatbelt
            raise RuntimeError("%s: github_desc have not been set" % self)

        return (mergify_state, github_state, github_desc)

    UNUSABLE_STATES = ["unknown", None]

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=0.2),
                    stop=tenacity.stop_after_attempt(5))
    def _ensure_mergable_state(self, force=False):
        if self.g_pull.merged:
            return
        if (not force and
                self.g_pull.mergeable_state not in self.UNUSABLE_STATES):
            return

        # Github is currently processing this PR, we wait the completion
        # TODO(sileht): We should be able to do better that retry 15x
        LOG.info("%s, refreshing...", self)

        # FIXME(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change...
        self.g_pull._headers.pop(Consts.RES_ETAG, None)
        self.g_pull._headers.pop(Consts.RES_LAST_MODIFIED, None)
        self.g_pull.update()
        if (self.g_pull.merged or
                self.g_pull.mergeable_state not in self.UNUSABLE_STATES):
            return
        raise tenacity.TryAgain

    def __lt__(self, other):
        return ((self.mergify_state, self.g_pull.updated_at) >
                (other.mergify_state, other.g_pull.updated_at))

    def __eq__(self, other):
        return ((self.mergify_state, self.g_pull.updated_at) ==
                (other.mergify_state, other.g_pull.updated_at))

    def base_is_modifiable(self):
        return (self.g_pull.raw_data["maintainer_can_modify"] or
                self.g_pull.head.repo.id == self.g_pull.base.repo.id)

    def merge(self, rule):
        try:
            self.g_pull.merge(sha=self.g_pull.head.sha,
                              merge_method=rule["merge_strategy"]["method"])
        except github.GithubException as e:   # pragma: no cover
            if (self.g_pull.is_merged() and
                    e.data["message"] == "Pull Request is not mergeable"):
                # Not a big deal, we will received soon the pull_request/close
                # event
                LOG.info("%s: was merged in the meantime", self)
                return True

            fallback = rule["merge_strategy"]["rebase_fallback"]
            if (e.data["message"] != "This branch can't be rebased" or
                    rule["merge_strategy"]["method"] != "rebase" or
                    fallback == "none"):
                LOG.error("%s merge fail: %d, %s",
                          self, e.status, e.data["message"], exc_info=True)
                return False

            # If rebase fail retry with merge
            try:
                self.g_pull.merge(sha=self.g_pull.head.sha,
                                  merge_method=fallback)
            except github.GithubException as e:
                LOG.error("%s merge fail: %d, %s",
                          self, e.status, e.data["message"], exc_info=True)
                return False

            # FIXME(sileht): depending on the kind of failure we can endloop
            # to try to merge the pr again and again.
            # to repoduce the issue
        return True

    def post_check_status(self, redis, installation_id, state, msg,
                          context=None):

        context = "pr" if context is None else context
        msg_key = "%s/%s/%d/%s" % (installation_id,
                                   self.g_pull.base.repo.full_name,
                                   self.g_pull.number, context)

        if len(msg) >= 140:
            description = msg[0:137] + "..."
            redis.hset("status", msg_key, msg.encode('utf8'))
            target_url = "%s/check_status_msg/%s" % (config.BASE_URL, msg_key)
        else:
            description = msg
            target_url = None

        LOG.info("%s set status to %s (%s)", self, state, description)
        # NOTE(sileht): We can't use commit.create_status() because
        # if use the head repo instead of the base repo
        try:
            self.g_pull._requester.requestJsonAndCheck(
                "POST",
                self.g_pull.base.repo.url + "/statuses/" +
                self.g_pull.head.sha,
                input={'state': state,
                       'description': description,
                       'target_url': target_url,
                       'context': "%s/%s" % (config.CONTEXT, context)},
                headers={'Accept':
                         'application/vnd.github.machine-man-preview+json'}
            )
        except github.GithubException as e:  # pragma: no cover
            LOG.error("%s set status fail: %s",
                      self, e.data["message"], exc_info=True)

    def __str__(self):
        required_statuses = {
            StatusState.FAILURE: "failure",
            StatusState.SUCCESS: "success",
            StatusState.PENDING: "pending",
            None: "notset"
        }[self._required_statuses]

        state = {
            MergifyState.NOT_READY: "not-ready",
            MergifyState.ALMOST_READY: "almost-ready",
            MergifyState.NEED_BRANCH_UPDATE:
            "need-branch-update",
            MergifyState.READY: "ready",
            None: "notset"
        }[self._mergify_state]

        return ("%(login)s/%(repo)s/pull/%(number)d@%(branch)s "
                "s:%(pr_state)s/%(statuses)s "
                "r:%(approvals)s/%(required_approvals)s "
                "-> %(mergify_state)s (%(github_state)s/%(github_desc)s)" % {
                    "login": self.g_pull.base.user.login,
                    "repo": self.g_pull.base.repo.name,
                    "number": self.g_pull.number,
                    "branch": self.g_pull.base.ref,
                    "pr_state": ("merged" if self.g_pull.merged else
                                 (self.g_pull.mergeable_state or "none")),
                    "statuses": required_statuses,
                    "approvals": ("notset" if self._approvals is None
                                  else len(self._approvals[0])),
                    "required_approvals": ("notset"
                                           if self._approvals is None
                                           else self._approvals[2]),
                    "mergify_state": state,
                    "github_state": ("notset"
                                     if self._github_state is None
                                     else self._github_state),
                    "github_desc": ("notset"
                                    if self._github_description is None
                                    else self._github_description),
                })
