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

import github

from mergify_engine import backports
from mergify_engine import config
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import rules
from mergify_engine import utils

LOG = logging.getLogger(__name__)

ENDING_STATES = ["failure", "error", "success"]


class MergifyEngine(object):
    def __init__(self, g, installation_id, installation_token, subscription,
                 user, repo):
        self._redis = utils.get_redis_for_cache()
        self._g = g
        self._installation_id = installation_id
        self._installation_token = installation_token
        self._subscription = subscription

        self._u = user
        self._r = repo

    def handle(self, event_type, data):
        # Everything start here

        incoming_pull = gh_pr.from_event(self._r, data)
        if not incoming_pull and event_type == "status":
            # It's safe to take the one from cache, since only status have
            # changed
            incoming_pull = self.get_incoming_pull_from_cache(data["sha"])
            if not incoming_pull:
                issues = list(self._g.search_issues("is:pr %s" % data["sha"]))
                if len(issues) >= 1:
                    incoming_pull = self._r.get_pull(issues[0].number)

        if not incoming_pull:
            LOG.info("No pull request found in the event %s, "
                     "ignoring" % event_type)
            return

        # Log the event
        self.log_formated_event(event_type, incoming_pull, data)

        if event_type == "status" and incoming_pull.head.sha != data["sha"]:
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        elif event_type == "status" and incoming_pull.merged:
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)")
            return

        # CHECK IF THE CONFIGURATION IS GOING TO CHANGE
        if (event_type == "pull_request"
                and data["action"] in ["opened", "synchronize"]
                and self._r.default_branch == incoming_pull.base.ref):
            ref = None
            for f in incoming_pull.get_files():
                if f.filename == ".mergify.yml":
                    ref = f.contents_url.split("?ref=")[1]

            if ref is not None:
                try:
                    rules.get_branch_rule(self._r, incoming_pull.base.ref, ref)
                except rules.InvalidRules as e:
                    # Not configured, post status check with the error message
                    incoming_pull.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id, "failure",
                        str(e), "future-config-checker")
                else:
                    incoming_pull.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id, "success",
                        "The new configuration is valid",
                        "future-config-checker")

        # BRANCH CONFIGURATION CHECKING
        branch_rule = None
        try:
            branch_rule = rules.get_branch_rule(self._r,
                                                incoming_pull.base.ref)
        except rules.NoRules as e:
            LOG.info("No need to proceed queue (.mergify.yml is missing)")
            return
        except rules.InvalidRules as e:
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                incoming_pull.mergify_engine_github_post_check_status(
                    self._redis, self._installation_id, "failure", str(e))
            return

        try:
            gh_branch.configure_protection_if_needed(
                self._r, incoming_pull.base.ref, branch_rule)
        except github.UnknownObjectException:
            LOG.exception("Fail to protect branch, disabled mergify")
            return

        if not branch_rule:
            LOG.info("Mergify disabled on branch %s", incoming_pull.base.ref)
            return

        # PULL REQUEST UPDATER

        fullify_extra = {
            # NOTE(sileht): Both are used by compute_approvals
            "branch_rule": branch_rule,
            "collaborators": [u.id for u in self._r.get_collaborators()]
        }

        if incoming_pull.state == "closed":
            self.cache_remove_pull(incoming_pull)
            self.proceed_queue(incoming_pull.base.ref, **fullify_extra)
            LOG.info("Just update cache (pull_request closed)")
            if incoming_pull.merged:
                backports.backports(self._r, incoming_pull,
                                    branch_rule["automated_backport_labels"],
                                    self._installation_token)
            if incoming_pull.head.ref.startswith("mergify/bp/%s" %
                                                 incoming_pull.base.ref):
                try:
                    self._r.get_git_ref("heads/%s" % incoming_pull.head.ref
                                        ).delete()
                    LOG.info("%s: branch %s deleted", incoming_pull.pretty(),
                             incoming_pull.head.ref)
                except github.UnknownObjectException:
                    pass

            return

        # First, remove informations we don't want to get from cache, so their
        # will be got/computed by PullRequest.fullify()
        if event_type == "refresh":
            cache = {}
        else:
            cache = self.get_cache_for_pull_number(incoming_pull.base.ref,
                                                   incoming_pull.number)
            cache = dict((k, v) for k, v in cache.items()
                         if k.startswith("mergify_engine_"))
            cache.pop("mergify_engine_weight", None)
            cache.pop("mergify_engine_status_desc", None)
            cache.pop("mergify_engine_weight_and_status", None)

            if event_type == "status":
                cache.pop("mergify_engine_combined_status", None)
                cache["mergify_engine_ci_statuses"] = {}
            elif event_type == "pull_request_review":
                cache.pop("mergify_engine_reviews", None)
                cache.pop("mergify_engine_approvals", None)
                cache.pop("mergify_engine_approved", None)
            elif (event_type == "pull_request" and
                  data["action"] == "synchronize"):
                    cache.pop("mergify_engine_combined_status", None)
                    cache["mergify_engine_ci_statuses"] = {}

        incoming_pull = incoming_pull.fullify(cache, **fullify_extra)
        self.cache_save_pull(incoming_pull)

        if (event_type == "pull_request_review" and
                data["review"]["user"]["id"] not in
                fullify_extra["collaborators"]):
            LOG.info("Just update cache (pull_request_review non-collab)")
            return

        # NOTE(sileht): PullRequest updated or comment posted, maybe we need to
        # update github
        # Get and refresh the queues
        if event_type in ["pull_request", "pull_request_review",
                          "refresh"]:
            incoming_pull.mergify_engine_github_post_check_status(
                self._redis, self._installation_id)

        self.proceed_queue(incoming_pull.base.ref, **fullify_extra)

    ###########################
    # State machine goes here #
    ###########################

    def build_queue(self, branch, **extra):
        data = self._redis.hgetall(self.get_cache_key(branch))

        with futures.ThreadPoolExecutor(max_workers=config.WORKERS) as tpe:
            pulls = list(tpe.map(
                lambda p: gh_pr.from_cache(self._r, json.loads(p), **extra),
                data.values()))

        sort_key = operator.attrgetter('mergify_engine_weight', 'updated_at')
        pulls = list(sorted(pulls, key=sort_key, reverse=True))
        LOG.info("%s, queues content:" % self._get_logprefix(branch))
        for p in pulls:
            LOG.info("%s, sha: %s->%s)", p.pretty(), p.base.sha, p.head.sha)
        return pulls

    def proceed_queue(self, branch, **extra):

        queue = self.build_queue(branch, **extra)
        # Proceed the queue
        if not queue:
            LOG.info("Nothing queued, skipping queues processing")
            return

        p = queue[0]

        LOG.info("%s selected", p.pretty())

        if p.mergify_engine_weight >= 11:
            # FIXME(sileht): If two PR are ready at the same times
            # this can fail, that's not a big deal, but we should ignore
            # the error here. And maybe try the next PR.
            if p.mergify_engine_merge(extra["branch_rule"]):
                # Wait for the closed event now
                LOG.info("%s -> merged", p.pretty())
            else:
                LOG.info("%s -> merge fail", p.pretty())

        elif p.mergify_engine_weight >= 5 and p.mergeable_state == "behind":
            if p.mergify_engine["combined_status"] == "success":
                # rebase it and wait the next pull_request event
                # (synchronize)
                if not self._subscription["token"]:
                    p.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id, "failure",
                        "No user access_token setuped for rebasing")
                    LOG.info("%s -> branch not updatable, token missing",
                             p.pretty())
                elif not p.base_is_modifiable:
                    p.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id, "failure",
                        "PR owner doesn't allow modification")
                    LOG.info("%s -> branch not updatable, token missing",
                             p.pretty())
                elif p.mergify_engine_update_branch(
                        self._subscription["token"]):
                    LOG.info("%s -> branch updated", p.pretty())
                else:
                    LOG.info("%s -> branch not updatable, "
                             "manual intervention required", p.pretty())
            else:
                LOG.info("%s -> github combined status != success", p.pretty())

        else:
            LOG.info("%s -> weight < 10", p.pretty())

    def cache_save_pull(self, pull):
        key = self.get_cache_key(pull.base.ref)
        self._redis.hset(key, pull.number, json.dumps(pull.jsonify()))
        self._redis.publish("update-%s" % self._installation_id, key)

    def cache_remove_pull(self, pull):
        key = self.get_cache_key(pull.base.ref)
        self._redis.hdel(key, pull.number)
        self._redis.publish("update-%s" % self._installation_id, key)

    def get_cache_for_pull_number(self, current_branch, number):
        key = self.get_cache_key(current_branch)
        p = self._redis.hget(key, number)
        return {} if p is None else json.loads(p)

    def get_cache_for_pull_sha(self, current_branch, sha):
        key = self.get_cache_key(current_branch)
        raw_pulls = self._redis.hgetall(key)
        for pull in raw_pulls.values():
            pull = json.loads(pull)
            if pull["head"]["sha"] == sha:
                return pull
        return {}

    def get_incoming_pull_from_cache(self, sha):
        for branch in self.get_cached_branches():
            incoming_pull = self.get_cache_for_pull_sha(branch, sha)
            if incoming_pull:
                return gh_pr.from_event(self._r, incoming_pull)

    def get_cache_key(self, branch):
        # Use only IDs, not name
        return "queues~%s~%s~%s~%s~%s" % (
            self._installation_id, self._u.login,
            self._r.name, self._r.private, branch)

    def get_cached_branches(self):
        return [b.split('~')[5] for b in
                self._redis.keys(self.get_cache_key("*"))]

    def _get_logprefix(self, branch="<unknown>"):
        return (self._u.login + "/" + self._r.name +
                "/pull/XXX@" + branch + " (-)")

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
        else:
            if incoming_pull:
                p_info = incoming_pull.pretty()
            else:
                p_info = self._get_logprefix()
            extra = ", ignored"

        LOG.info("***********************************************************")
        LOG.info("%s received event '%s'%s", p_info, event_type, extra)
        if config.LOG_RATELIMIT:
            rate = self._g.get_rate_limit().rate
            LOG.info("%s ratelimit: %s/%s, reset at %s", p_info,
                     rate.remaining, rate.limit, rate.reset)
