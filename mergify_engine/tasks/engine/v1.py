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
import enum
import fnmatch
import functools
import itertools
import json
from concurrent import futures

import attr

import daiquiri

import github

import tenacity

import uhashring

from mergify_engine import backports
from mergify_engine import branch_protection
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.worker import app


LOG = daiquiri.getLogger(__name__)


RING = uhashring.HashRing(
    nodes=list(itertools.chain.from_iterable(
        map(lambda x: "worker-%003d@%s" % (x, fqdn), range(w))
        for fqdn, w in sorted(config.TOPOLOGY.items())
    )))


@app.task
def handle(installation_id, subscription,
           branch_rules, event_type, data, event_pull_raw):
    # NOTE(sileht): The processor is not concurrency safe, so a repo is always
    # sent to the same worker.
    # This work in coordination with app.conf.worker_direct = True that creates
    # a dedicated queue on exchange c.dq2 for each worker
    routing_key = RING.get_node(data["repository"]["full_name"])
    LOG.info("Sending repo %s to %s", data["repository"]["full_name"],
             routing_key)
    _handle.s(installation_id, subscription,
              branch_rules, event_type, data, event_pull_raw
              ).apply_async(exchange='C.dq2', routing_key=routing_key)


@app.task
def _handle(installation_id, subscription,
            branch_rules, event_type, data, event_pull_raw):
    installation_token = utils.get_installation_token(installation_id)
    if not installation_token:
        return
    pull = MergifyPullV1.from_raw(installation_id,
                                  installation_token,
                                  event_pull_raw)
    MergifyEngine(installation_id, installation_token, subscription,
                  pull.g_pull.base.repo).handle(
                      branch_rules, event_type, data, pull)


# Use IntEnum to be able to sort pull requests based on this state.
class MergifyState(enum.IntEnum):
    NOT_READY = 0
    NEED_BRANCH_UPDATE = 10
    ALMOST_READY = 20
    READY = 30

    def __str__(self):
        return self._name_.lower().replace("_", "-")


class StatusState(enum.Enum):
    FAILURE = "failure"
    SUCCESS = "success"
    PENDING = "pending"

    def __str__(self):
        return self.value


@functools.total_ordering
@attr.s(cmp=False)
class MergifyPullV1(mergify_pull.MergifyPull):
    _complete = attr.ib(init=False, default=False)
    _reviews_required = attr.ib(init=False, default=None)

    # Cached attributes
    _reviews_ok = attr.ib(init=False, default=None)
    _reviews_ko = attr.ib(init=False, default=None)
    _required_statuses = attr.ib(
        init=False, default=None,
        validator=attr.validators.optional(
            attr.validators.instance_of(StatusState)),
    )
    _mergify_state = attr.ib(
        init=False, default=None,
        validator=attr.validators.optional(
            attr.validators.instance_of(MergifyState)),
    )
    _github_state = attr.ib(init=False, default=None)
    _github_description = attr.ib(init=False, default=None)

    def __lt__(self, other):
        return ((self.mergify_state, self.g_pull.updated_at) >
                (other.mergify_state, other.g_pull.updated_at))

    def __eq__(self, other):
        return ((self.mergify_state, self.g_pull.updated_at) ==
                (other.mergify_state, other.g_pull.updated_at))

    def jsonify(self):
        raw = copy.copy(self.g_pull.raw_data)
        raw["mergify_engine_reviews_ok"] = self._reviews_ok
        raw["mergify_engine_reviews_ko"] = self._reviews_ko
        raw["mergify_engine_required_statuses"] = self._required_statuses.value
        raw["mergify_engine_status"] = {
            "mergify_state": self._mergify_state.value,
            "github_description": self._github_description,
            "github_state": self._github_state
        }
        return raw

    def _ensure_complete(self):
        if not self._complete:  # pragma: no cover
            raise RuntimeError("%s: used an incomplete MergifyPullV1")

    @property
    def status(self):
        # TODO(sileht): Should be removed at some point. When the cache
        # will not have mergify_engine_status key anymore
        return {"mergify_state": self.mergify_state,
                "github_state": self.github_state,
                "github_description": self.github_description}

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

    def complete(self, cache, branch_rule, collaborators):
        need_to_be_saved = False

        protection = branch_rule["protection"]
        if protection["required_pull_request_reviews"]:
            self._reviews_required = protection[
                "required_pull_request_reviews"][
                    "required_approving_review_count"]
        else:
            self._reviews_required = 0

        if "mergify_engine_reviews_ok" in cache:
            self._reviews_ok = cache["mergify_engine_reviews_ok"]
            self._reviews_ko = cache["mergify_engine_reviews_ko"]
        else:
            need_to_be_saved = True
            self._reviews_ok, self._reviews_ko = self._compute_approvals(
                branch_rule, collaborators)

        if "mergify_engine_required_statuses" in cache:
            self._required_statuses = StatusState(cache[
                "mergify_engine_required_statuses"])
        else:
            need_to_be_saved = True
            self._required_statuses = self._compute_required_statuses(
                branch_rule)

        if "mergify_engine_status" in cache:
            s = cache["mergify_engine_status"]
            self._mergify_state = MergifyState(s["mergify_state"])
            self._github_state = s["github_state"]
            self._github_description = s["github_description"]
        else:
            need_to_be_saved = True
            (self._mergify_state, self._github_state,
             self._github_description) = self._compute_status(
                 branch_rule, collaborators)

        self._complete = True
        return need_to_be_saved

    def refresh(self, branch_rule, collaborators):
        # NOTE(sileht): Redownload the PULL to ensure we don't have
        # any etag floating around
        self._ensure_mergable_state(force=True)
        return self.complete({}, branch_rule, collaborators)

    def _compute_approvals(self, branch_rule, collaborators):
        """Compute approvals.

        :param branch_rule: The rule for the considered branch.
        :param collaborators: The list of collaborators.
        :return: A tuple (users_with_review_ok, users_with_review_ko)
        """
        users_info = {}
        reviews_ok = set()
        reviews_ko = set()
        for review in self.g_pull.get_reviews():
            if review.user.id not in collaborators:
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
                self.log.error("review state unhandled",
                               state=review.state)

        return ([users_info[u] for u in reviews_ok],
                [users_info[u] for u in reviews_ko])

    @staticmethod
    def _find_required_context(contexts, generic_check):
        for c in contexts:
            if generic_check.context.startswith(c):
                return c

    def _compute_required_statuses(self, branch_rule):
        # return True is CIs succeed, False is their fail, None
        # is we don't known yet.
        # FIXME(sileht): I don't use a Enum yet to not
        protection = branch_rule["protection"]
        if not protection["required_status_checks"]:
            return StatusState.SUCCESS

        # NOTE(sileht): Due to the difference of both API we use only success
        # bellow.
        contexts = set(protection["required_status_checks"]["contexts"])
        seen_contexts = set()

        for check in self._get_checks():
            required_context = self._find_required_context(contexts, check)
            if required_context:
                seen_contexts.add(required_context)
                if check.state in ["pending", None]:
                    return StatusState.PENDING
                elif check.state != "success":
                    return StatusState.FAILURE

        if contexts - seen_contexts:
            return StatusState.PENDING
        else:
            return StatusState.SUCCESS

    def _disabled_by_rules(self, branch_rule):
        labels = [l.name for l in self.g_pull.labels]

        enabling_label = branch_rule["enabling_label"]
        if enabling_label is not None and enabling_label not in labels:
            return "Disabled — enabling label missing"

        if branch_rule["disabling_label"] in labels:
            return "Disabled — disabling label present"

        g_pull_files = [f.filename for f in self.g_pull.get_files()]
        for w in branch_rule["disabling_files"]:
            filtered = fnmatch.filter(g_pull_files, w)
            if filtered:
                return ("Disabled — %s is modified"
                        % filtered[0])
        return None

    def _compute_status(self, branch_rule, collaborators):
        disabled = self._disabled_by_rules(branch_rule)

        mergify_state = MergifyState.NOT_READY
        github_state = "pending"
        github_desc = None

        if disabled:
            github_state = "failure"
            github_desc = disabled

        elif self._reviews_ko:
            github_desc = "Change requests need to be dismissed"

        elif (self._reviews_required and
              len(self._reviews_ok) < self._reviews_required):
            github_desc = (
                "%d/%d approvals required" %
                (len(self._reviews_ok), self._reviews_required)
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
                protection = branch_rule["protection"]
                if (protection["required_pull_request_reviews"] and
                    protection["required_pull_request_reviews"]
                    ["require_code_owner_reviews"] and
                    (self.g_pull._rawData['requested_teams'] or
                     self.g_pull._rawData['requested_reviewers'])):
                    github_desc = "Waiting for code owner review"

                else:
                    # NOTE(sileht): assume it's the Github bug and the PR is
                    # ready, if it's not the merge button will just fail.
                    self.log.warning("mergeable_state is unexpected, "
                                     "trying to merge the pull request")
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
        else:  # pragma: no cover
            raise RuntimeError("%s: Unexpected mergify_state" % self)

        if github_desc is None:  # pragma: no cover
            # Seatbelt
            raise RuntimeError("%s: github_desc have not been set" % self)

        return (mergify_state, github_state, github_desc)

    def post_check_status(self, state, msg, context=None):

        redis = utils.get_redis_for_cache()
        context = "pr" if context is None else context
        msg_key = "%s/%s/%d/%s" % (self.installation_id,
                                   self.g_pull.base.repo.full_name,
                                   self.g_pull.number, context)

        if len(msg) >= 140:
            description = msg[0:137] + "..."
            redis.hset("status", msg_key, msg.encode('utf8'))
            target_url = "%s/check_status_msg/%s" % (config.BASE_URL, msg_key)
        else:
            description = msg
            target_url = None

        self.log.info("set status", state=state, description=description)
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
            self.log.error("set status failed",
                           error=e.data["message"], exc_info=True)

    def set_and_post_error(self, github_description):
        self._mergify_state = MergifyState.NOT_READY
        self._github_state = "failure"
        self._github_description = github_description
        self.post_check_status(self.github_state,
                               self.github_description)

    def __str__(self):
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
                    "statuses": str(self._required_statuses),
                    "approvals": ("notset" if self._reviews_ok is None
                                  else len(self._reviews_ok)),
                    "required_approvals": ("notset"
                                           if self._reviews_required is None
                                           else self._reviews_required),
                    "mergify_state": str(self._mergify_state),
                    "github_state": ("notset"
                                     if self._github_state is None
                                     else self._github_state),
                    "github_desc": ("notset"
                                    if self._github_description is None
                                    else self._github_description),
                })


@attr.s
class Caching(object):
    repository = attr.ib()
    installation_id = attr.ib()
    installation_token = attr.ib()
    _redis = attr.ib(factory=utils.get_redis_for_cache, init=False)

    def _get_logprefix(self, branch="<unknown>"):
        return (self.repository.owner.login + "/" + self.repository.name +
                "/pull/XXX@" + branch + " (-)")

    def _get_cache_key(self, branch):
        # Use only IDs, not name
        return "queues~%s~%s~%s~%s~%s" % (
            self.installation_id, self.repository.owner.login.lower(),
            self.repository.name.lower(), self.repository.private, branch)

    def _cache_save_pull(self, pull):
        key = self._get_cache_key(pull.g_pull.base.ref)
        self._redis.hset(key, pull.g_pull.number, json.dumps(pull.jsonify()))

    def _cache_remove_pull(self, pull):
        key = self._get_cache_key(pull.g_pull.base.ref)
        self._redis.hdel(key, pull.g_pull.number)

    def _get_cached_branches(self):
        return [b.split('~')[5] for b in
                self._redis.keys(self._get_cache_key("*"))]

    def get_cache_for_pull_number(self, current_branch, number):
        key = self._get_cache_key(current_branch)
        p = self._redis.hget(key, number)
        return {} if p is None else json.loads(p)

    def get_pr_for_pull_number(self, current_branch, number):
        p = self.get_cache_for_pull_number(current_branch, number)
        if p:
            return github.PullRequest.PullRequest(
                self.repository._requester, {}, p,
                completed=True)

    def get_pr_for_sha(self, sha):
        for branch in self._get_cached_branches():
            incoming_pull = self._get_cache_for_pull_sha(branch, sha)
            if incoming_pull:
                return github.PullRequest.PullRequest(
                    self.repository._requester, {}, incoming_pull,
                    completed=True)

    def _get_cache_for_pull_sha(self, current_branch, sha):
        key = self._get_cache_key(current_branch)
        raw_pulls = self._redis.hgetall(key)
        for pull in raw_pulls.values():
            pull = json.loads(pull)
            if pull["head"]["sha"] == sha:
                return pull
        return {}  # pragma: no cover


class MergifyEngine(Caching):
    def __init__(self, installation_id, installation_token,
                 subscription, repo):
        super(MergifyEngine, self).__init__(
            repository=repo, installation_id=installation_id,
            installation_token=installation_token)
        self._subscription = subscription

    def handle(self, branch_rules, event_type, data, incoming_pull):
        # Everything start here
        incoming_branch = incoming_pull.g_pull.base.ref
        incoming_state = incoming_pull.g_pull.state

        try:
            branch_rule = rules.get_branch_rule(branch_rules, incoming_branch)
        except rules.InvalidRules as e:  # pragma: no cover
            # Not configured, post status check with the error message
            if (event_type == "pull_request" and
                    data["action"] in ["opened", "synchronize"]):
                check_api.set_check_run(
                    incoming_pull.g_pull, "current-config-checker",
                    "completed", "failure", output={
                        "title": "The Mergify configuration is invalid",
                        "summary": str(e)
                    })

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
                backports.backport_from_labels(
                    incoming_pull,
                    branch_rule["automated_backport_labels"],
                    self.installation_token)

            if event_type == "pull_request" and data["action"] == "closed":
                self.get_processor().proceed_queue(
                    incoming_branch, branch_rule, collaborators)

                if not incoming_pull.g_pull.merged:
                    incoming_pull.post_check_status(
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
        # will be recomputed by MergifyPullV1.complete()
        if event_type == "refresh":
            cache = {}
            old_status = None
        else:
            cache = self.get_cache_for_pull_number(incoming_branch,
                                                   incoming_pull.g_pull.number)
            cache = dict((k, v) for k, v in cache.items()
                         if k.startswith("mergify_engine_"))
            old_status = cache.pop("mergify_engine_status", None)
            if event_type in ["status", "check_run", "check_suite"]:
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
                incoming_pull.github_state,
                incoming_pull.github_description,
            )

        self.get_processor().proceed_queue(
            incoming_branch, branch_rule, collaborators)

    def get_processor(self):
        return Processor(subscription=self._subscription,
                         repository=self.repository,
                         installation_id=self.installation_id,
                         installation_token=self.installation_token)


class Processor(Caching):
    def __init__(self, subscription, repository, installation_id,
                 installation_token):
        super(Processor, self).__init__(repository=repository,
                                        installation_id=installation_id,
                                        installation_token=installation_token)
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
        LOG.info("%s, queues content:", self._get_logprefix(branch))
        for p in pulls:
            LOG.info("sha: %s->%s",
                     p.g_pull.base.sha, p.g_pull.head.sha,
                     pull_request=p)
        return pulls

    def _load_from_cache_and_complete(self, data, branch_rule, collaborators):
        data = json.loads(data)
        pull = MergifyPullV1.from_raw(self.installation_id,
                                      self.installation_token,
                                      data)
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

            if p.mergify_state == MergifyState.NOT_READY:
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

        if p.mergify_state == MergifyState.READY:
            p.post_check_status("success", "Merged")

            if p.merge(branch_rule["merge_strategy"]["method"],
                       branch_rule["merge_strategy"]["rebase_fallback"]):
                # Wait for the closed event now
                LOG.info("merged", pull_request=p)
            else:  # pragma: no cover
                p.set_and_post_error("Merge fail")
                self._cache_save_pull(p)
                raise tenacity.TryAgain

        elif p.mergify_state == MergifyState.ALMOST_READY:
            LOG.info("waiting for final statuses completion", pull_request=p)

        elif p.mergify_state == MergifyState.NEED_BRANCH_UPDATE:
            if branch_updater.update(p, self._subscription["token"]):
                # Wait for the synchronize event now
                LOG.info("branch updated", pull_request=p)
            else:  # pragma: no cover
                p.set_and_post_error("contributor branch is not updatable, "
                                     "manual update/rebase required.")
                self._cache_save_pull(p)
                raise tenacity.TryAgain
