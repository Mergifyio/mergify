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
import dataclasses

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import logs
from mergify_engine import sub_utils
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.clients import github


LOG = logs.getLogger(__name__)


@dataclasses.dataclass
class Queue:
    installation_id: int
    owner: str
    repo: str
    ref: str

    DEFAULT_MERGE_METHOD = "merge"

    @property
    def log(self):
        return logs.getLogger(
            __name__, gh_owner=self.owner, gh_repo=self.repo, gh_branch=self.ref
        )

    @classmethod
    def from_queue_name(cls, name):
        _, installation_id, owner, repo, branch = name.split("~")
        return cls(int(installation_id), owner, repo, branch)

    @classmethod
    def from_context(cls, ctxt):
        return cls(
            ctxt.client.installation["id"],
            ctxt.pull["base"]["repo"]["owner"]["login"],
            ctxt.pull["base"]["repo"]["name"],
            ctxt.pull["base"]["ref"],
        )

    @property
    def _cache_key(self):
        return f"strict-merge-queues~{self.installation_id}~{self.owner.lower()}~{self.repo.lower()}~{self.ref}"

    def _method_cache_key(self, pull_number):
        return f"strict-merge-method~{self.installation_id}~{self.owner.lower()}~{self.repo.lower()}~{pull_number}"

    def get_merge_method(
        self, pull_number: int, default: str = DEFAULT_MERGE_METHOD
    ) -> str:
        """Return merge method for a pull request.

        :param pull_number: The pull request number.
        :param default: The default method to return if not found.
        """
        redis = utils.get_redis_for_cache()
        return redis.get(self._method_cache_key(pull_number)) or default

    def add_pull(self, pull_number, method):
        redis = utils.get_redis_for_cache()
        score = utils.utcnow().timestamp()
        redis.zadd(self._cache_key, {pull_number: score}, nx=True)
        redis.set(self._method_cache_key(pull_number), method)
        self.log.info("pull request added to merge queue", gh_pull=pull_number)

    def remove_pull(self, pull_number):
        redis = utils.get_redis_for_cache()
        redis.zrem(self._cache_key, pull_number)
        redis.delete(self._method_cache_key(pull_number))
        self.log.info("pull request removed from merge queue", gh_pull=pull_number)

    def _move_pull_at_end(self, pull_number):  # pragma: no cover
        redis = utils.get_redis_for_cache()
        score = utils.utcnow().timestamp()
        redis.zadd(self._cache_key, {pull_number: score}, xx=True)
        self.log.info(
            "pull request moved at the end of the merge queue", gh_pull=pull_number
        )

    def _move_pull_to_new_base_branch(self, pull_number, old_base_branch):
        redis = utils.get_redis_for_cache()
        old_queue = self.__class__(
            self.installation_id, self.owner, self.repo, old_base_branch, pull_number
        )
        redis.zrem(old_queue._cache_key, pull_number)
        # FIXME do not get method to set it back again
        method = self.get_merge_method(pull_number)
        self.add_pull(pull_number, method)
        self.log.info(
            "pull request moved from queue %s to this queue",
            old_queue,
            gh_pull=pull_number,
        )

    def get_pulls(self):
        redis = utils.get_redis_for_cache()
        return redis.zrange(self._cache_key, 0, -1)

    def delete_queue(self):
        redis = utils.get_redis_for_cache()
        redis.delete(self._cache_key)

    def handle_first_pull_in_queue(self, ctxt):
        old_checks = [
            c for c in ctxt.pull_engine_check_runs if c["name"].endswith(" (merge)")
        ]

        output = helpers.merge_report(ctxt, True)
        if output:
            conclusion, title, summary = output
            ctxt.log.info(
                "pull request closed in the meantime",
                conclusion=conclusion,
                title=title,
                summary=summary,
            )
            self.remove_pull(ctxt.pull["number"])
        else:
            ctxt.log.info("updating base branch of pull request")
            method = self.get_merge_method(ctxt.pull["number"])
            conclusion, title, summary = helpers.update_pull_base_branch(ctxt, method)

            if ctxt.pull["state"] == "closed":
                ctxt.log.info(
                    "pull request closed in the meantime",
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                )
                self.remove_pull(ctxt.pull["number"])
            elif conclusion == "failure":
                ctxt.log.info("base branch update failed", title=title, summary=summary)
                self._move_pull_at_end(ctxt.pull["number"])

        status = "completed" if conclusion else "in_progress"
        for c in old_checks:
            check_api.set_check_run(
                ctxt,
                c["name"],
                status,
                conclusion,
                output={"title": title, "summary": summary},
            )

    @classmethod
    def process_queues(cls):
        # NOTE(sileht): Don't use the celery retry mechanism here, the
        # periodic tasks already retries. This ensure a repo can't block
        # another one.
        redis = utils.get_redis_for_cache()
        LOG.info("smart strict workflow loop start")
        for queue_name in redis.keys("strict-merge-queues~*"):
            queue = cls.from_queue_name(queue_name)
            queue.log.info("handling queue")
            try:
                queue.process()
            except Exception:
                queue.log.error("Fail to process merge queue", exc_info=True)
        LOG.info("smart strict workflow loop end")

    def process(self):
        pull_numbers = self.get_pulls()

        self.log.info("%d pulls queued", len(pull_numbers), queue=list(pull_numbers))

        if not pull_numbers:
            return

        pull_number = int(pull_numbers[0])

        try:
            installation = github.get_installation(
                self.owner, self.repo, self.installation_id
            )
        except exceptions.MergifyNotInstalled:
            self.delete_queue()
            return

        subscription = sub_utils.get_subscription(
            utils.get_redis_for_cache(), self.installation_id,
        )

        with github.get_client(self.owner, self.repo, installation) as client:
            data = client.item(f"pulls/{pull_number}")

            try:
                ctxt = context.Context(client, data, subscription)
            except exceptions.RateLimited as e:
                self.log.debug("rate limited", remaining_seconds=e.countdown)
                return
            except exceptions.MergeableStateUnknown as e:  # pragma: no cover
                e.ctxt.log.warning(
                    "pull request with mergeable_state unknown retrying later",
                )
                self._move_pull_at_end(pull_number)
                return
            try:
                if ctxt.pull["base"]["ref"] != self.ref:
                    ctxt.log.info(
                        "pull request base branch have changed",
                        old_branch=self.ref,
                        new_branch=ctxt.pull["base"]["ref"],
                    )
                    self._move_pull_to_new_base_branch(ctxt, self.ref)
                elif ctxt.pull["state"] == "closed" or ctxt.is_behind:
                    # NOTE(sileht): Pick up this pull request and rebase it again
                    # or update its status and remove it from the queue
                    ctxt.log.info(
                        "pull request needs to be updated again or has been closed",
                    )
                    self.handle_first_pull_in_queue(ctxt)
                else:
                    # NOTE(sileht): Pull request has not been merged or cancelled
                    # yet wait next loop
                    ctxt.log.info("pull request checks are still in progress")
            except Exception:  # pragma: no cover
                ctxt.log.error("Fail to process merge queue", exc_info=True)
                self._move_pull_at_end(pull_number)
