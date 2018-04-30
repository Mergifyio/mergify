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
import requests

from mergify_engine import config
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import rules
from mergify_engine import utils

LOG = logging.getLogger(__name__)

ENDING_STATES = ["failure", "error", "success"]


class MergifyEngine(object):
    def __init__(self, g, installation_id, user, repo):
        self._redis = utils.get_redis()
        self._g = g
        self._installation_id = installation_id

        self._u = user
        self._r = repo

    def get_updater_token(self):
        updater_token = self._redis.get("installation-token-%s" %
                                        self._installation_id)
        if updater_token is None:
            LOG.info("Token for %s not cached, retrieving it..." %
                     self._installation_id)
            resp = requests.get("https://mergify.io/engine/token/%s" %
                                self._installation_id,
                                auth=(config.OAUTH_CLIENT_ID,
                                      config.OAUTH_CLIENT_SECRET))
            updater_token = resp.json()['access_token']
            self._redis.set("installation-token-%s" % self._installation_id,
                            updater_token)
        return updater_token

    def handle(self, event_type, data):
        # Everything start here

        if event_type == "status":
            # Don't compute the queue for nothing
            if data["context"].startswith("%s/" % config.CONTEXT):
                return
            elif data["context"] == "continuous-integration/travis-ci/push":
                return

        # Get the current pull request
        incoming_pull = gh_pr.from_event(self._r, data)
        if not incoming_pull and event_type == "status":
            # It's safe to take the one from cache, since only status have
            # changed
            incoming_pull = self.get_incoming_pull_from_cache(data["sha"])
            if not incoming_pull:
                issues = list(self._g.search_issues("is:pr %s" %
                                                    data["sha"]))
                if len(issues) >= 1:
                    incoming_pull = self._r.get_pull(issues[0].number)

        if not incoming_pull:
            LOG.info("No pull request found in the event %s, "
                     "ignoring" % event_type)
            return

        # Log the event
        self.log_formated_event(event_type, incoming_pull, data)

        # Don't handle private repo for now
        if self._r.private:
            LOG.info("No need to proceed queue (private repo)")
            return

        # Unhandled and already logged
        if event_type not in ["pull_request", "pull_request_review",
                              "status", "refresh"]:
            LOG.info("No need to proceed queue (unwanted event_type)")
            return

        elif event_type == "status" and incoming_pull.head.sha != data["sha"]:
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        # We don't care about *labeled/*assigned/review_request*/edited
        elif (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize", "edited"]):
            LOG.info("No need to proceed queue (unwanted pull_request action)")
            return

        elif incoming_pull.state == "closed":
            self.cache_remove_pull(incoming_pull)
            LOG.info("Just update cache (pull_request closed)")
            return

        # BRANCH CONFIGURATION CHECKING
        branch_rule = None
        try:
            branch_rule = rules.get_branch_rule(
                self._r, incoming_pull.base.ref)
        except rules.NoRules as e:
            # Not configured, post status check with the error message
            incoming_pull.mergify_engine_github_post_check_status(
                self._redis, self._installation_id, str(e))
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

        if (event_type == "status" and
                data["context"] == "continuous-integration/travis-ci/pr"):
            fullify_extra["travis"] = data

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

            if (event_type == "status" and
                    data["state"] == cache.get(
                        "mergify_engine_travis_state")):
                LOG.info("No need to proceed queue (got status without "
                         "state change '%s')" % data["state"])
                return
            elif event_type == "status":
                cache.pop("mergify_engine_combined_status", None)
                cache["mergify_engine_ci_statuses"] = {}
                cache["mergify_engine_travis_state"] = data["state"]
                cache["mergify_engine_travis_url"] = data["target_url"]
                if data["state"] in ENDING_STATES:
                    cache.pop("mergify_engine_travis_detail", None)
                else:
                    cache["mergify_engine_travis_detail"] = {}

            elif event_type == "pull_request_review":
                cache.pop("mergify_engine_reviews", None)
                cache.pop("mergify_engine_approvals", None)
                cache.pop("mergify_engine_approved", None)
            elif event_type == "pull_request":
                if data["action"] not in ["closed", "edited"]:
                    cache.pop("mergify_engine_commits", None)
                if data["action"] == "synchronize":
                    # NOTE(sileht): hardcode ci status that will be refresh
                    # on next travis event
                    cache.pop("mergify_engine_combined_status", None)
                    cache["mergify_engine_ci_statuses"] = {}
                    cache.pop("mergify_engine_travis_state", None)
                    cache.pop("mergify_engine_travis_url", None)
                    cache.pop("mergify_engine_travis_detail", None)

        incoming_pull = incoming_pull.fullify(cache, **fullify_extra)
        self.cache_save_pull(incoming_pull)

        # NOTE(sileht): just refresh this pull request in cache
        if event_type == "status" and data["state"] == "pending":
            LOG.info("Just update cache (ci status pending)")
            return
        elif event_type == "pull_request" and data["action"] == "edited":
            LOG.info("Just update cache (pull_request edited)")
            return
        elif (event_type == "pull_request_review" and
                data["review"]["user"]["id"] not in
                fullify_extra["collaborators"]):
            LOG.info("Just update cache (pull_request_review non-collab)")
            return

        # TODO(sileht): Disable that until we can configure it in the yml file
        # NOTE(sileht): We check the state of incoming_pull and the event
        # because user can have restart a travis job between the event
        # received and when we looks at it with travis API
        #  if (event_type == "status"
        #          and data["state"] in ENDING_STATES
        #          and data["context"] in ["continuous-integration/travis-ci",
        #                                  "continuous-integration/travis-ci/pr"]
        #          and incoming_pull.mergify_engine["travis_state"]
        #          in ENDING_STATES
        #          and incoming_pull.mergify_engine["travis_detail"]):
        #      incoming_pull.mergify_engine_travis_post_build_results()

        # NOTE(sileht): PullRequest updated or comment posted, maybe we need to
        # update github
        # Get and refresh the queues
        if event_type in ["pull_request", "pull_request_review",
                          "refresh"]:
            incoming_pull.mergify_engine_github_post_check_status(
                self._redis, self._installation_id)

        # NOTE(sileht): Starting here cache should not be updated
        queue = self.build_queue(incoming_pull.base.ref)
        # Proceed the queue
        if queue:
            # protect the branch before doing anything
            self.proceed_queue(queue[0])
        else:
            LOG.info("Nothing queued, skipping queues processing")

    ###########################
    # State machine goes here #
    ###########################

    def build_queue(self, branch):
        data = self._redis.hgetall(self.get_cache_key(branch))

        with futures.ThreadPoolExecutor(max_workers=config.WORKERS) as tpe:
            pulls = list(tpe.map(
                lambda p: gh_pr.from_cache(self._r,
                                           json.loads(p.decode("utf8"))),
                data.values()))

        sort_key = operator.attrgetter('mergify_engine_weight', 'updated_at')
        pulls = list(sorted(pulls, key=sort_key, reverse=True))
        LOG.info("%s, queues content:" % self._get_logprefix(branch))
        for p in pulls:
            LOG.info("%s, sha: %s->%s)", p.pretty(), p.base.sha, p.head.sha)
        return pulls

    def proceed_queue(self, p):
        """Do the next action for this pull request

        'p' is the top priority pull request to merge
        """

        LOG.info("%s selected", p.pretty())

        if p.mergify_engine_weight >= 11:
            if p.mergify_engine_merge():
                # Wait for the closed event now
                LOG.info("%s -> merged", p.pretty())
            else:
                LOG.info("%s -> merge fail", p.pretty())

        elif p.mergeable_state == "behind":
            if p.mergify_engine["combined_status"] == "success":
                # rebase it and wait the next pull_request event
                # (synchronize)
                updater_token = self.get_updater_token()
                if not updater_token:
                    p.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id,
                        "No user access_token setuped for rebasing")
                    LOG.info("%s -> branch not updatable, token missing",
                             p.pretty())
                elif not p.maintainer_can_modify:
                    p.mergify_engine_github_post_check_status(
                        self._redis, self._installation_id,
                        "PR owner doesn't allow modification")
                    LOG.info("%s -> branch not updatable, token missing",
                             p.pretty())
                elif p.mergify_engine_update_branch(updater_token):
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

    def cache_remove_pull(self, pull):
        key = self.get_cache_key(pull.base.ref)
        self._redis.hdel(key, pull.number)

    def get_cache_for_pull_number(self, current_branch, number):
        key = self.get_cache_key(current_branch)
        p = self._redis.hget(key, number)
        return {} if p is None else json.loads(p.decode("utf8"))

    def get_cache_for_pull_sha(self, current_branch, sha):
        key = self.get_cache_key(current_branch)
        raw_pulls = self._redis.hgetall(key)
        for pull in raw_pulls.values():
            pull = json.loads(pull.decode("utf8"))
            if pull["head"]["sha"] == sha:
                return pull
        return {}

    def get_incoming_pull_from_cache(self, sha):
        for branch in self.get_cached_branches():
            incoming_pull = self.get_cache_for_pull_sha(branch, sha)
            if incoming_pull:
                return gh_pr.from_event(self._r, incoming_pull)

    def get_cache_key(self, branch):
        return "queues~%s~%s~%s~%s" % (self._installation_id, self._u.login,
                                       self._r.name, branch)

    def get_cached_branches(self):
        return [b.decode('utf8').split('~')[4] for b in
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
