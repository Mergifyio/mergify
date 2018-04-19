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

from mergify_engine import config
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import utils

LOG = logging.getLogger(__name__)

ENDING_STATES = ["failure", "error", "success"]


class PastaMakerEngine(object):
    def __init__(self, g, installation_id, user, repo):
        self._redis = utils.get_redis()
        self._g = g
        self._installation_id = installation_id
        self._updater_token = self._redis.get("installation-token-%s" %
                                              self._installation_id)
        self._u = user
        self._r = repo

    def handle(self, event_type, data):
        # Everything start here

        # Don't handle private repo for now
        if self._r.private:
            return

        if event_type == "status":
            # Don't compute the queue for nothing
            if data["context"].startswith("%s/" % config.CONTEXT):
                return
            elif data["context"] == "continuous-integration/travis-ci/push":
                return

        # Get the current pull request
        incoming_pull = gh_pr.from_event(self._r, data)
        if not incoming_pull:
            if event_type == "status":
                # It's safe to take the one from cache, since only status have
                # changed
                incoming_pull = self.get_incoming_pull_from_cache(data["sha"])
                if not incoming_pull:
                    issues = list(self._g.search_issues("is:pr %s" %
                                                        data["sha"]))
                    if len(issues) >= 1:
                        incoming_pull = self._r.get_pull(issues[0].number)

            elif (event_type == "refresh" and
                  data["refresh_ref"].startswith("pull/")):
                incoming_pull = self._r.get_pull(int(data["refresh_ref"][5:]))

        # Get the current branch
        current_branch = None
        if incoming_pull:
            current_branch = incoming_pull.base.ref
        elif (event_type == "refresh" and
              data["refresh_ref"].startswith("branch/")):
            current_branch = data["refresh_ref"][7:]
        else:
            LOG.info("No pull request or branch found in the event %s, "
                     "ignoring" % event_type)
            return

        # Log the event
        self.log_formated_event(event_type, incoming_pull, data)

        # Unhandled and already logged
        if event_type not in ["pull_request", "pull_request_review",
                              "status", "refresh"]:
            LOG.info("No need to proceed queue (unwanted event_type)")
            return

        if event_type == "status" and incoming_pull.head.sha != data["sha"]:
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        # We don't care about *labeled/*assigned/review_request*/edited
        if (event_type == "pull_request" and data["action"] not in [
                "opened", "reopened", "closed", "synchronize", "edited"]):
            LOG.info("No need to proceed queue (unwanted pull_request action)")
            return

        try:
            branch_policy_error = None
            branch_policy = gh_branch.get_branch_policy(self._r,
                                                        current_branch)
        except gh_branch.NoPolicies as e:
            branch_policy_error = str(e)
            branch_policy = None

        fullify_extra = {
            "branch_policy": branch_policy,
            "collaborators": [u.id for u in self._r.get_collaborators()]
        }

        if (event_type == "status" and
                data["context"] == "continuous-integration/travis-ci/pr"):
            fullify_extra["travis"] = data

        # Retrieve cache
        cached_pulls = self.load_cache(current_branch)

        # Gather missing github/travis information and compute weight
        if incoming_pull:
            # First, remove informations we don't want to get from cache
            if event_type == "refresh":
                cache = {}
            else:
                cache = self.get_cache_for_pull(cached_pulls,
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

        # NOTE(sileht): just refresh this pull request in cache
        if event_type == "status" and data["state"] == "pending":
            self.build_queue_and_save_to_cache(cached_pulls, current_branch,
                                               incoming_pull)
            LOG.info("Just update cache (ci status pending)")
            return
        elif event_type == "pull_request" and data["action"] == "edited":
            self.build_queue_and_save_to_cache(cached_pulls, current_branch,
                                               incoming_pull)
            LOG.info("Just update cache (pull_request edited)")
            return
        elif (event_type == "pull_request_review" and
                data["review"]["user"]["id"] not in
                fullify_extra["collaborators"]):
            self.build_queue_and_save_to_cache(cached_pulls, current_branch,
                                               incoming_pull)
            LOG.info("Just update cache (pull_request_review non-collab)")
            return

        # NOTE(sileht): We check the state of incoming_pull and the event
        # because user can have restart a travis job between the event
        # received and when we looks at it with travis API
        if (event_type == "status"
                and data["state"] in ENDING_STATES
                and data["context"] in ["continuous-integration/travis-ci",
                                        "continuous-integration/travis-ci/pr"]
                and incoming_pull.mergify_engine["travis_state"]
                in ENDING_STATES
                and incoming_pull.mergify_engine["travis_detail"]):
            incoming_pull.mergify_engine_travis_post_build_results()

        # NOTE(sileht): PullRequest updated or comment posted, maybe we need to
        # update github
        # Get and refresh the queues
        if not incoming_pull:
            queues = self.get_updated_queues_from_github(
                current_branch, **fullify_extra)
            if event_type == "refresh":
                for p in queues:
                    p.mergify_engine_github_post_check_status(
                        self._installation_id, self._updater_token,
                        branch_policy_error)
            else:
                LOG.warning("FIXME: We got a event without incoming_pull:"
                            "%s : %s" % (event_type, data))
        else:
            if event_type in ["pull_request", "pull_request_review",
                              "refresh"]:
                incoming_pull.mergify_engine_github_post_check_status(
                    self._installation_id, self._updater_token,
                    branch_policy_error)
            queues = self.build_queue_and_save_to_cache(cached_pulls,
                                                        current_branch,
                                                        incoming_pull)

        # Proceed the queue
        if branch_policy and queues:
            # protect the branch before doing anything
            try:
                gh_branch.protect_if_needed(self._r, current_branch,
                                            branch_policy)
            except github.UnknownObjectException:
                LOG.exception("Fail to protect branch, disabled automerge")
                return
            self.proceed_queues(queues)
        elif not branch_policy:
            LOG.info("No policies setuped, skipping queues processing")
        else:
            LOG.info("Nothing queued, skipping queues processing")

    ###########################
    # State machine goes here #
    ###########################

    def proceed_queues(self, queues):
        """Do the next action for this pull request

        'p' is the top priority pull request to merge
        """

        p = queues[0]
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
                if p.mergify_engine_update_branch(self._updater_token):
                    LOG.info("%s -> branch updated", p.pretty())
                else:
                    LOG.info("%s -> branch not updatable, "
                             "manual intervention required", p.pretty())
            else:
                LOG.info("%s -> github combined status != success", p.pretty())

        else:
            LOG.info("%s -> weight < 10", p.pretty())

    def set_cache_queues(self, branch, raw_pulls):
        key = "queues~%s~%s~%s" % (self._u.login, self._r.name, branch)
        LOG.info("%s, saving %d pulls to cache (%s)",
                 self._get_logprefix(branch), len(raw_pulls),
                 [p["number"] for p in raw_pulls])
        if raw_pulls:
            payload = json.dumps(raw_pulls)
            self._redis.set(key, payload)
        else:
            self._redis.delete(key)
        self._redis.publish("update", key)

    def get_cache_for_pull(self, raw_pulls, number=None, sha=None):
        for pull in raw_pulls:
            if sha is not None and pull["head"]["sha"] == sha:
                return pull
            if number is not None and pull["number"] == number:
                return pull
        return {}

    def get_incoming_pull_from_cache(self, sha):
        for branch in self.get_cached_branches():
            cached_pulls = self.load_cache(branch)
            incoming_pull = self.get_cache_for_pull(cached_pulls, sha=sha)
            if incoming_pull:
                return gh_pr.from_event(self._r, incoming_pull)

    def get_cached_branches(self):
        cache_key = "queues~%s~%s~*" % (self._u.login, self._r.name)
        return [b.split('~')[3] for b in self._redis.keys(cache_key)]

    def load_cache(self, branch):
        cache_key = "queues~%s~%s~%s" % (self._u.login, self._r.name,
                                         branch)
        data = self._redis.get(cache_key)
        if data:
            return json.loads(data)
        else:
            return []

    def build_queue_and_save_to_cache(self, pulls, branch, incoming_pull):
        LOG.info("%s, load %d pulls from cache (%s)",
                 incoming_pull.pretty(),
                 len(pulls),
                 [p["number"] for p in pulls])

        pulls = [p for p in pulls if int(p["number"]) != incoming_pull.number]

        LOG.info("%s, convert %d pulls from cache (%s)",
                 incoming_pull.pretty(),
                 len(pulls),
                 [p["number"] for p in pulls])

        with futures.ThreadPoolExecutor(max_workers=config.WORKERS) as tpe:
            pulls = list(tpe.map(lambda p: gh_pr.from_cache(self._r, p),
                                 pulls))

        if incoming_pull.state == "open":
            LOG.info("%s, add #%s to cache", incoming_pull.pretty(),
                     incoming_pull.number)
            pulls.append(incoming_pull)
        return self.sort_save_and_log_queues(branch, pulls)

    def get_updated_queues_from_github(self, branch, **extra):
        LOG.info("%s, retrieving pull requests", self._get_logprefix(branch))
        pulls = self._r.get_pulls(sort="created", direction="asc", base=branch)
        with futures.ThreadPoolExecutor(max_workers=config.WORKERS) as tpe:
            list(tpe.map(lambda p: p.fullify(**extra), pulls))
        return self.sort_save_and_log_queues(branch, pulls)

    def sort_save_and_log_queues(self, branch, pulls):
        sort_key = operator.attrgetter('mergify_engine_weight', 'updated_at')
        pulls = list(sorted(pulls, key=sort_key, reverse=True))
        LOG.info("%s, cache content:" % self._get_logprefix(branch))
        for p in pulls:
            LOG.info("%s, sha: %s->%s)", p.pretty(), p.base.sha, p.head.sha)
        raw_queues = [p.jsonify() for p in pulls]
        self.set_cache_queues(branch, raw_queues)
        return pulls

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
            if incoming_pull:
                p_info = incoming_pull.pretty()
            else:
                p_info = self._get_logprefix(data["refresh_ref"])
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
