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

from concurrent import futures
import json
import logging
import operator

import attr
import github

from mergify_engine import backports
from mergify_engine import config
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import gh_pr_fullifier
from mergify_engine import rules
from mergify_engine import utils

LOG = logging.getLogger(__name__)

ENDING_STATES = ["failure", "error", "success"]


@attr.s
class Caching(object):
    user = attr.ib()
    repository = attr.ib()
    installation_id = attr.ib()
    _redis = attr.ib()

    def _get_logprefix(self, branch="<unknown>"):
        return (self.user.login + "/" + self.repository.name +
                "/pull/XXX@" + branch + " (-)")

    def _get_cache_key(self, branch):
        # Use only IDs, not name
        return "queues~%s~%s~%s~%s~%s" % (
            self.installation_id, self.user.login.lower(),
            self.repository.name.lower(), self.repository.private, branch)

    def _cache_save_pull(self, pull):
        key = self._get_cache_key(pull.base.ref)
        self._redis.hset(key, pull.number, json.dumps(pull.jsonify()))

    def _cache_remove_pull(self, pull):
        key = self._get_cache_key(pull.base.ref)
        self._redis.hdel(key, pull.number)


class MergifyEngine(Caching):
    def __init__(self, g, installation_id, installation_token, subscription,
                 user, repo):
        super(MergifyEngine, self).__init__(user=user,
                                            repository=repo,
                                            installation_id=installation_id,
                                            redis=utils.get_redis_for_cache())
        self._g = g
        self._installation_token = installation_token
        self._subscription = subscription

    def get_cache_for_pull_number(self, current_branch, number):
        key = self._get_cache_key(current_branch)
        p = self._redis.hget(key, number)
        return {} if p is None else json.loads(p)

    def get_cached_incoming_pull_for_pull_sha(self, sha):
        for branch in self.get_cached_branches():
            incoming_pull = self.get_cache_for_pull_sha(branch, sha)
            if incoming_pull:
                return github.PullRequest.PullRequest(
                    self.repository._requester, {}, incoming_pull,
                    completed=True)

    def get_cached_branches(self):
        return [b.split('~')[5] for b in
                self._redis.keys(self._get_cache_key("*"))]

    def get_cache_for_pull_sha(self, current_branch, sha):
        key = self._get_cache_key(current_branch)
        raw_pulls = self._redis.hgetall(key)
        for pull in raw_pulls.values():
            pull = json.loads(pull)
            if pull["head"]["sha"] == sha:
                return pull
        return {}  # pragma: no cover

    def get_incoming_pull_for_sha(self, sha):
        incoming_pull = self.get_cached_incoming_pull_for_pull_sha(sha)
        if incoming_pull:
            return incoming_pull

        LOG.info("%s: sha not in cache", sha)
        issues = list(self._g.search_issues("is:pr %s" % sha))
        if not issues:
            LOG.info("%s: sha not attached to a pull request", sha)
            return
        if len(issues) > 1:
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning("%s: sha attached to multiple pull requests", sha)
        for i in issues:
            try:
                incoming_pull = self.repository.get_pull(i.number)
            except github.UnknownObjectException:  # pragma: no cover
                pass
            if incoming_pull and not incoming_pull.merged:
                return incoming_pull

    def get_incoming_pull_from_event(self, event_type, data):
        if "pull_request" in data:
            return github.PullRequest.PullRequest(
                self.repository._requester, {},
                data["pull_request"], completed=True
            )
        elif event_type == "status":
            return self.get_incoming_pull_for_sha(data["sha"])

    def handle(self, event_type, data):
        # Everything start here

        incoming_pull = self.get_incoming_pull_from_event(event_type, data)
        self.log_formated_event(event_type, incoming_pull, data)

        if not incoming_pull:  # pragma: no cover
            LOG.info("No pull request found in the event %s, "
                     "ignoring" % event_type)
            return

        if (event_type == "status" and
                incoming_pull.head.sha != data["sha"]):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        elif event_type == "status" and incoming_pull.merged:
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)")
            return

        # CHECK IF THE CONFIGURATION IS GOING TO CHANGE
        if (event_type == "pull_request"
                and data["action"] in ["opened", "synchronize"]
                and self.repository.default_branch == incoming_pull.base.ref):
            ref = None
            for f in incoming_pull.get_files():
                if f.filename == ".mergify.yml":
                    ref = f.contents_url.split("?ref=")[1]

            if ref is not None:
                try:
                    rules.get_branch_rule(
                        self.repository, incoming_pull.base.ref, ref
                    )
                except rules.InvalidRules as e:  # pragma: no cover
                    # Not configured, post status check with the error message
                    incoming_pull.mergify_engine_github_post_check_status(
                        self._redis, self.installation_id, "failure",
                        str(e), "future-config-checker")
                else:
                    incoming_pull.mergify_engine_github_post_check_status(
                        self._redis, self.installation_id, "success",
                        "The new configuration is valid",
                        "future-config-checker")

        # BRANCH CONFIGURATION CHECKING
        branch_rule = None
        try:
            branch_rule = rules.get_branch_rule(self.repository,
                                                incoming_pull.base.ref)
        except rules.NoRules as e:
            LOG.info("No need to proceed queue (.mergify.yml is missing)")
            return
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                incoming_pull.mergify_engine_github_post_check_status(
                    self._redis, self.installation_id, "failure", str(e))
            return

        try:
            gh_branch.configure_protection_if_needed(self.repository,
                                                     incoming_pull.base.ref,
                                                     branch_rule)
        except github.GithubException as e:  # pragma: no cover
            if e.status == 404 and e.data["message"] == "Branch not found":
                LOG.info("%s: branch no longer exists: %s",
                         incoming_pull.pretty(),
                         e.message)
                return
            raise

        if not branch_rule:
            LOG.info("Mergify disabled on branch %s", incoming_pull.base.ref)
            return

        # PULL REQUEST UPDATER

        fullify_extra = {
            # NOTE(sileht): Both are used by compute_approvals
            "branch_rule": branch_rule,
            "collaborators": [
                u.id for u in self.repository.get_collaborators()
            ],
        }

        if incoming_pull.state == "closed":
            self._cache_remove_pull(incoming_pull)
            LOG.info("Just update cache (pull_request closed)")

            if (event_type == "pull_request" and
                    data["action"] in ["closed", "labeled"] and
                    incoming_pull.merged):
                backports.backports(
                    self.repository, incoming_pull,
                    branch_rule["automated_backport_labels"],
                    self._installation_token)

            if event_type == "pull_request" and data["action"] == "closed":
                self.get_processor().proceed_queue(
                    incoming_pull.base.ref, **fullify_extra)

                if not incoming_pull.merged:
                    incoming_pull.mergify_engine_github_post_check_status(
                        self._redis, self.installation_id, "success",
                        "Pull request closed unmerged")

                if incoming_pull.head.ref.startswith("mergify/bp/%s" %
                                                     incoming_pull.base.ref):
                    try:
                        self.repository.get_git_ref(
                            "heads/%s" % incoming_pull.head.ref
                        ).delete()
                        LOG.info("%s: branch %s deleted",
                                 incoming_pull.pretty(),
                                 incoming_pull.head.ref)
                    except github.UnknownObjectException:  # pragma: no cover
                        pass

            return

        # First, remove informations we don't want to get from cache, so their
        # will be got/computed by PullRequest.fullify()
        if event_type == "refresh":
            cache = {}
            old_status = None
        else:
            cache = self.get_cache_for_pull_number(incoming_pull.base.ref,
                                                   incoming_pull.number)
            cache = dict((k, v) for k, v in cache.items()
                         if k.startswith("mergify_engine_"))
            old_status = cache.pop("mergify_engine_status", None)
            if event_type == "status":
                cache.pop("mergify_engine_required_statuses", None)
            elif event_type == "pull_request_review":
                cache.pop("mergify_engine_approvals", None)
            elif (event_type == "pull_request" and
                  data["action"] == "synchronize"):
                    cache.pop("mergify_engine_required_statuses", None)

        incoming_pull.fullify(cache, **fullify_extra)
        self._cache_save_pull(incoming_pull)

        if (event_type == "pull_request_review" and
                data["review"]["user"]["id"] not in
                fullify_extra["collaborators"]):
            LOG.info("Just update cache (pull_request_review non-collab)")
            return

        # NOTE(sileht): PullRequest updated or comment posted, maybe we need to
        # update github
        # Get and refresh the queues
        if old_status != incoming_pull.mergify_engine["status"]:
            incoming_pull.mergify_engine_github_post_check_status(
                self._redis, self.installation_id,
                incoming_pull.mergify_engine["status"]["github_state"],
                incoming_pull.mergify_engine["status"]["github_description"],
            )

        self.get_processor().proceed_queue(
            incoming_pull.base.ref, **fullify_extra)

    def get_processor(self):
        return Processor(user=self.user,
                         repository=self.repository,
                         installation_id=self.installation_id,
                         redis=self._redis)

    def log_formated_event(self, event_type, incoming_pull, data):
        if event_type == "pull_request":
            p_info = incoming_pull.pretty()
            extra = ", action: %s" % data["action"]

        elif event_type == "pull_request_review":
            p_info = incoming_pull.pretty()
            extra = ", action: %s, review-state: %s" % (
                data["action"], data["review"]["state"])

        elif event_type == "pull_request_review_comment":
            p_info = incoming_pull.pretty()
            extra = ", action: %s, review-state: %s" % (
                data["action"], data["comment"]["position"])

        elif event_type == "status":
            if incoming_pull:
                p_info = incoming_pull.pretty()
            else:
                p_info = self._get_logprefix()
            extra = ", ci-status: %s, sha: %s" % (data["state"], data["sha"])

        elif event_type == "refresh":
            p_info = incoming_pull.pretty()
            extra = ""
        else:  # pragma: no cover
            if incoming_pull:
                p_info = incoming_pull.pretty()
            else:
                p_info = self._get_logprefix()
            extra = ", ignored"

        LOG.info("***********************************************************")
        LOG.info("%s received event '%s'%s", p_info, event_type, extra)
        if config.LOG_RATELIMIT:  # pragma: no cover
            rate = self._g.get_rate_limit().rate
            LOG.info("%s ratelimit: %s/%s, reset at %s", p_info,
                     rate.remaining, rate.limit, rate.reset)


class Processor(Caching):

    @staticmethod
    def _sort_pulls(pulls):
        """Sort pull requests by state and updated_at"""
        sort_key = operator.attrgetter('mergify_engine_sort_status',
                                       'updated_at')
        return list(sorted(pulls, key=sort_key, reverse=True))

    def _build_queue(self, branch, **extra):
        """Return the pull requests from redis cache ordered by sort status"""

        data = self._redis.hgetall(self._get_cache_key(branch))

        with futures.ThreadPoolExecutor(
                max_workers=config.FETCH_WORKERS) as tpe:
            pulls = list(tpe.map(
                lambda p: gh_pr.from_cache(
                    self.repository, json.loads(p), **extra
                ),
                data.values()))
        pulls = self._sort_pulls(pulls)
        LOG.info("%s, queues content:" % self._get_logprefix(branch))
        for p in pulls:
            LOG.info("%s, sha: %s->%s)", p.pretty(), p.base.sha, p.head.sha)
        return pulls

    def _get_next_pull_to_processed(self, branch, **extra):
        """Return the next pull request to proceed

        This take the pull request with the higher status that is not yet
        closed.
        """

        queue = self._build_queue(branch, **extra)
        while queue:
            p = queue.pop(0)

            expected_state = p.mergify_engine["status"]["mergify_state"]

            # NOTE(sileht): We refresh it before processing, because the cache
            # can be outdated, user may have manually merged the PR or
            # mergify_state may have changed by an event not yet received.

            # FIXME(sileht): This will refresh the first pull request of the
            # queue on each event. To limit this almost useless refresh, we
            # should be smarted on when we call proceed_queue()
            p.fullify(force=True, **extra)

            if p.state == "closed":
                # NOTE(sileht): PR merged in the meantime or manually
                self._cache_remove_pull(p)
            elif expected_state != p.mergify_engine["status"]["mergify_state"]:
                # NOTE(sileht): The state have changed, put back the pull into
                # the queue and resort it
                queue.append(p)
                queue = self._sort_pulls(queue)
            else:
                # We found the next pull request to proceed
                self._cache_save_pull(p)
                return p

    def proceed_queue(self, branch, **extra):

        p = self._get_next_pull_to_processed(branch, **extra)
        if not p:
            LOG.info("Nothing queued, skipping queues processing")
            return

        LOG.info("%s selected", p.pretty())

        state = p.mergify_engine["status"]["mergify_state"]

        if state == gh_pr_fullifier.MergifyState.READY:
            p.mergify_engine_github_post_check_status(
                self._redis, self.installation_id,
                "success", "Merged")

            if p.mergify_engine_merge(extra["branch_rule"]):
                # Wait for the closed event now
                LOG.info("%s -> merged", p.pretty())
            else:
                LOG.info("%s -> merge fail", p.pretty())
                p.mergify_engine_github_post_check_status(
                    self._redis, self.installation_id,
                    "failure", "Merge fail")

        elif state == gh_pr_fullifier.MergifyState.NEED_BRANCH_UPDATE:
            # rebase it and wait the next pull_request event
            # (synchronize)
            if not self._subscription["token"]:  # pragma: no cover
                p.mergify_engine_github_post_check_status(
                    self._redis, self.installation_id, "failure",
                    "No user access_token setuped for rebasing")
                LOG.info("%s -> branch not updatable, token missing",
                         p.pretty())
            elif not p.base_is_modifiable:  # pragma: no cover
                p.mergify_engine_github_post_check_status(
                    self._redis, self.installation_id, "failure",
                    "PR owner doesn't allow modification")
                LOG.info("%s -> branch not updatable, base not modifiable",
                         p.pretty())
            elif p.mergify_engine_update_branch(
                    self._subscription["token"]):
                LOG.info("%s -> branch updated", p.pretty())
            else:
                LOG.info("%s -> branch not updatable, "
                         "manual intervention required", p.pretty())
        else:
            LOG.info("%s -> nothing to do (state: %s)", p.pretty(), state)
