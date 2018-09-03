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

import json
from concurrent import futures

import attr

import daiquiri

import github

import tenacity

from mergify_engine import backports
from mergify_engine import branch_protection
from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine import stats
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


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
        key = self._get_cache_key(pull.g_pull.base.ref)
        self._redis.hset(key, pull.g_pull.number, json.dumps(pull.jsonify()))

    def _cache_remove_pull(self, pull):
        key = self._get_cache_key(pull.g_pull.base.ref)
        self._redis.hdel(key, pull.g_pull.number)


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

    def get_cached_github_pull_for_pull_sha(self, sha):
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

    def get_github_pull_for_sha(self, sha):
        pull = self.get_cached_github_pull_for_pull_sha(sha)
        if pull:
            return pull

        issues = list(self._g.search_issues("is:pr %s" % sha))
        if not issues:
            return
        if len(issues) > 1:
            # NOTE(sileht): It's that technically possible, but really ?
            LOG.warning("sha attached to multiple pull requests", sha=sha)
        for i in issues:
            try:
                pull = self.repository.get_pull(i.number)
            except github.GithubException as e:  # pragma: no cover
                if e.status != 404:
                    raise
            if pull and not pull.merged:
                return pull

    def get_github_pull_from_event(self, event_type, data):
        if "pull_request" in data:
            return github.PullRequest.PullRequest(
                self.repository._requester, {},
                data["pull_request"], completed=True
            )
        elif event_type == "status":
            return self.get_github_pull_for_sha(data["sha"])

    def handle(self, event_type, data):
        # Everything start here

        event_pull = self.get_github_pull_from_event(event_type, data)

        if not event_pull:  # pragma: no cover
            self.log_formated_event(event_type, None, data)
            LOG.info("No pull request found in the event %s, "
                     "ignoring" % event_type)
            return

        incoming_pull = mergify_pull.MergifyPull(event_pull)
        incoming_branch = incoming_pull.g_pull.base.ref
        incoming_sha = incoming_pull.g_pull.head.sha
        incoming_state = incoming_pull.g_pull.state

        self.log_formated_event(event_type, incoming_pull, data)

        if event_type == "pull_request" and data["action"] == "open":
            stats.PULL_REQUESTS.inc()

        if (event_type == "status" and
                incoming_sha != data["sha"]):  # pragma: no cover
            LOG.info("No need to proceed queue (got status of an old commit)")
            return

        elif event_type == "status" and incoming_pull.g_pull.merged:
            LOG.info("No need to proceed queue (got status of a merged "
                     "pull request)")
            return

        # CHECK IF THE CONFIGURATION IS GOING TO CHANGE
        if (event_type == "pull_request" and
           data["action"] in ["opened", "synchronize"] and
           self.repository.default_branch == incoming_branch):
            ref = None
            for f in incoming_pull.g_pull.get_files():
                if f.filename == ".mergify.yml":
                    ref = f.contents_url.split("?ref=")[1]

            if ref is not None:
                try:
                    config = rules.get_mergify_config(
                        self.repository, ref=ref)
                    rules.get_branch_rule(config['rules'], incoming_branch)
                except rules.InvalidRules as e:  # pragma: no cover
                    # Not configured, post status check with the error message
                    # FIXME()!!!!!!!!!!!
                    incoming_pull.post_check_status(
                        self._redis, self.installation_id,
                        "failure", str(e), "future-config-checker")
                else:
                    incoming_pull.post_check_status(
                        self._redis, self.installation_id,
                        "success", "The new configuration is valid",
                        "future-config-checker")

        # BRANCH CONFIGURATION CHECKING
        try:
            config = rules.get_mergify_config(self.repository)
            branch_rule = rules.get_branch_rule(config['rules'],
                                                incoming_branch)
        except rules.NoRules as e:
            LOG.info("No need to proceed queue (.mergify.yml is missing)")
            return
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                incoming_pull.post_check_status(
                    self._redis, self.installation_id,
                    "failure", str(e))
            return

        try:
            branch_protection.configure_protection_if_needed(
                self.repository, incoming_branch, branch_rule)
        except github.GithubException as e:  # pragma: no cover
            if e.status == 404 and e.data["message"] == "Branch not found":
                LOG.info("head branch no longer exists",
                         pull_request=incoming_pull)
                return
            raise

        if not branch_rule:
            LOG.info("Mergify disabled on branch", branch=incoming_branch)
            return

        # PULL REQUEST UPDATER

        collaborators = [u.id for u in self.repository.get_collaborators()]

        if incoming_state == "closed":
            self._cache_remove_pull(incoming_pull)
            LOG.info("Just update cache (pull request closed)")

            if (event_type == "pull_request" and
                    data["action"] in ["closed", "labeled"] and
                    incoming_pull.g_pull.merged):
                backports.backports(
                    self.repository, incoming_pull,
                    branch_rule["automated_backport_labels"],
                    self._installation_token)

            if event_type == "pull_request" and data["action"] == "closed":
                self.get_processor().proceed_queue(
                    incoming_branch, branch_rule, collaborators)

                if not incoming_pull.g_pull.merged:
                    incoming_pull.post_check_status(
                        self._redis, self.installation_id,
                        "success", "Pull request closed unmerged")

                head_branch = incoming_pull.g_pull.head.ref
                if head_branch.startswith("mergify/bp/%s" % incoming_branch):
                    try:
                        self.repository.get_git_ref(
                            "heads/%s" % head_branch
                        ).delete()
                        LOG.info("branch deleted",
                                 pull_request=incoming_pull,
                                 branch=head_branch)
                    except github.GithubException as e:  # pragma: no cover
                        if e.status != 404:
                            raise

            return

        # First, remove informations we don't want to get from cache, so their
        # will be recomputed by MergifyPull.complete()
        if event_type == "refresh":
            cache = {}
            old_status = None
        else:
            cache = self.get_cache_for_pull_number(incoming_branch,
                                                   incoming_pull.g_pull.number)
            cache = dict((k, v) for k, v in cache.items()
                         if k.startswith("mergify_engine_"))
            old_status = cache.pop("mergify_engine_status", None)
            if event_type == "status":
                cache.pop("mergify_engine_required_statuses", None)
            elif event_type == "pull_request_review":
                cache.pop("mergify_engine_reviews_ok", None)
                cache.pop("mergify_engine_reviews_ko", None)
            elif (event_type == "pull_request" and
                  data["action"] == "synchronize"):
                    cache.pop("mergify_engine_required_statuses", None)

        changed = incoming_pull.complete(cache, branch_rule, collaborators)
        if changed:
            self._cache_save_pull(incoming_pull)

        if (event_type == "pull_request_review" and
                data["review"]["user"]["id"] not in collaborators):
            LOG.info("Just update cache (pull_request_review non-collab)")
            return

        # NOTE(sileht): PullRequest updated or comment posted, maybe we need to
        # update github
        # Get and refresh the queues
        if old_status != incoming_pull.status:
            incoming_pull.post_check_status(
                self._redis, self.installation_id,
                incoming_pull.github_state,
                incoming_pull.github_description,
            )

        self.get_processor().proceed_queue(
            incoming_branch, branch_rule, collaborators)

    def get_processor(self):
        return Processor(subscription=self._subscription,
                         user=self.user,
                         repository=self.repository,
                         installation_id=self.installation_id,
                         redis=self._redis)

    def log_formated_event(self, event_type, incoming_pull, data):
        if not incoming_pull:
            incoming_pull = self._get_logprefix()

        if event_type == "pull_request":
            detail = ", action: %s" % data["action"]

        elif event_type == "pull_request_review":
            detail = ", action: %s, review-state: %s" % (
                data["action"], data["review"]["state"])

        elif event_type == "pull_request_review_comment":
            detail = ", action: %s, review-state: %s" % (
                data["action"], data["comment"]["position"])

        elif event_type == "status":
            detail = ", ci-status: %s, sha: %s" % (data["state"], data["sha"])

        elif event_type == "refresh":
            detail = ""
        else:  # pragma: no cover
            detail = ", unknown"

        LOG.info("***********************************************************")
        LOG.info("received event '%s'%s", event_type, detail,
                 pull_request=incoming_pull)
        if config.LOG_RATELIMIT:  # pragma: no cover
            rate = self._g.get_rate_limit().rate
            LOG.info("ratelimit: %s/%s, reset at %s",
                     rate.remaining, rate.limit, rate.reset,
                     pull_request=incoming_pull)


class Processor(Caching):
    def __init__(self, subscription, user, repository, installation_id, redis):
        super(Processor, self).__init__(user=user,
                                        repository=repository,
                                        installation_id=installation_id,
                                        redis=redis)
        self._subscription = subscription

    def _build_queue(self, branch, branch_rule, collaborators):
        """Return the pull requests from redis cache ordered by sort status."""
        data = self._redis.hgetall(self._get_cache_key(branch))

        with futures.ThreadPoolExecutor(
                max_workers=config.FETCH_WORKERS) as tpe:
            pulls = sorted(tpe.map(
                lambda p: self._load_from_cache_and_complete(
                    p, branch_rule, collaborators),
                data.values()))
        LOG.info("%s, queues content:" % self._get_logprefix(branch))
        for p in pulls:
            LOG.info("sha: %s->%s",
                     p.g_pull.base.sha, p.g_pull.head.sha,
                     pull_request=p)
        return pulls

    def _load_from_cache_and_complete(self, data, branch_rule, collaborators):
        data = json.loads(data)
        pull = mergify_pull.MergifyPull(github.PullRequest.PullRequest(
            self.repository._requester, {}, data, completed=True))
        changed = pull.complete(data, branch_rule, collaborators)
        if changed:
            self._cache_save_pull(pull)
        return pull

    def _get_next_pull_to_processed(self, branch, branch_rule, collaborators):
        """Return the next pull request to proceed.

        This take the pull request with the higher status that is not yet
        closed.
        """
        queue = self._build_queue(branch, branch_rule, collaborators)

        while queue:
            p = queue.pop(0)

            if p.mergify_state == mergify_pull.MergifyState.NOT_READY:
                continue

            expected_state = p.mergify_state

            # NOTE(sileht): We refresh it before processing, because the cache
            # can be outdated, user may have manually merged the PR or
            # mergify_state may have changed by an event not yet received.

            # FIXME(sileht): This will refresh the first pull request of the
            # queue on each event. To limit this almost useless refresh, we
            # should be smarted on when we call proceed_queue()
            p.refresh(branch_rule, collaborators)
            self._cache_save_pull(p)

            if p.g_pull.state == "closed":
                # NOTE(sileht): PR merged in the meantime or manually
                self._cache_remove_pull(p)
            elif expected_state != p.mergify_state:
                # NOTE(sileht): The state have changed, put back the pull into
                # the queue and resort it
                queue.append(p)
                queue.sort()
            else:
                return p

    @tenacity.retry(retry=tenacity.retry_never)
    def proceed_queue(self, branch, branch_rule, collaborators):

        p = self._get_next_pull_to_processed(
            branch, branch_rule, collaborators)
        if not p:
            LOG.info("nothing to do",
                     repository=self.repository.full_name,
                     branch=branch)
            return

        if p.mergify_state == mergify_pull.MergifyState.READY:
            p.post_check_status(self._redis, self.installation_id,
                                "success", "Merged")

            if p.merge(branch_rule["merge_strategy"]["method"],
                       branch_rule["merge_strategy"]["rebase_fallback"]):
                # Wait for the closed event now
                LOG.info("merged", pull_request=p)
            else:  # pragma: no cover
                p.set_and_post_error(self._redis, self.installation_id,
                                     "Merge fail")
                self._cache_save_pull(p)
                raise tenacity.TryAgain

        elif p.mergify_state == mergify_pull.MergifyState.ALMOST_READY:
            LOG.info("waiting for final statuses completion", pull_request=p)

        elif p.mergify_state == mergify_pull.MergifyState.NEED_BRANCH_UPDATE:
            if branch_updater.update(p, self._subscription["token"]):
                # Wait for the synchronize event now
                LOG.info("branch updated", pull_request=p)
            else:  # pragma: no cover
                p.set_and_post_error(self._redis, self.installation_id,
                                     "contributor branch is not updatable, "
                                     "manual update/rebase required.")
                self._cache_save_pull(p)
                raise tenacity.TryAgain
