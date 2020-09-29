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
import asyncio
import dataclasses
import json

import daiquiri
import redis

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.actions.merge import helpers
from mergify_engine.clients import github


LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class Queue:
    redis: redis.Redis
    installation_id: int
    owner: str
    repo: str
    ref: str

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__, gh_owner=self.owner, gh_repo=self.repo, gh_branch=self.ref
        )

    @classmethod
    def from_queue_name(cls, redis, name):
        _, installation_id, owner, repo, branch = name.split("~")
        return cls(redis, int(installation_id), owner, repo, branch)

    @classmethod
    def from_context(cls, ctxt):
        return cls(
            utils.get_redis_for_cache(),
            ctxt.client.auth.installation["id"],
            ctxt.pull["base"]["repo"]["owner"]["login"],
            ctxt.pull["base"]["repo"]["name"],
            ctxt.pull["base"]["ref"],
        )

    @property
    def _cache_key(self):
        return f"strict-merge-queues~{self.installation_id}~{self.owner}~{self.repo}~{self.ref}"

    def _config_cache_key(self, pull_number):
        return f"strict-merge-config~{self.installation_id}~{self.owner}~{self.repo}~{pull_number}"

    # FIXME(sileht): delete me in a couple of week
    def _old_config_cache_key(self, pull_number):
        return f"strict-merge-config~{self.installation_id}~{self.owner.lower()}~{self.repo.lower()}~{pull_number}"

    def get_config(self, pull_number: int) -> dict:
        """Return merge config for a pull request.

        :param pull_number: The pull request number.
        """
        config = self.redis.get(self._config_cache_key(pull_number))
        if config is None:
            config = self.redis.get(self._old_config_cache_key(pull_number))
        if config is None:
            # FIXME(sileht): We should never ever pass here in theory, but
            # Currently we can have race condition like:
            # * smart queue coro: get next PR
            # * engine coro: get merge event
            # * engine coro: cleanup queue with 3 redis cmds without transaction
            # * smart queue coro: get merge config (get_config()), and got None.
            # That's not a huge deal
            # TODO(sileht): Everything about queue should be done in redis transaction
            # e.g.: add/update/get/del of a pull in queue
            return {
                "strict_method": "merge",
                "priority": 2000,
                "effective_priority": 2000,
                "bot_account": None,
            }
        config = json.loads(config)
        # TODO(sileht): for compatibility purpose, we can drop that in a couple of week
        config.setdefault("effective_priority", config["priority"])
        config.setdefault("bot_account", None)
        return config

    def _add_pull(self, pull_number, priority, update=False):
        """Add a pull without setting its method.

        :param update: If update is True, don't create PR if it's not there.
        """
        score = utils.utcnow().timestamp() / priority
        if update:
            flags = dict(xx=True)
        else:
            flags = dict(nx=True)
        self.redis.zadd(self._cache_key, {pull_number: score}, **flags)

    def add_pull(self, ctxt, config):
        config = config.copy()
        config["effective_priority"] = config["priority"]

        if not ctxt.subscription.has_feature(subscription.Features.PRIORITY_QUEUES):
            config["effective_priority"] = helpers.PriorityAliases.medium.value

        self.redis.set(
            self._config_cache_key(ctxt.pull["number"]),
            json.dumps(config),
        )
        self._add_pull(ctxt.pull["number"], config["effective_priority"])
        self.log.info(
            "pull request added to merge queue",
            gh_pull=ctxt.pull["number"],
            config=config,
        )

    def _remove_pull(self, pull_number):
        """Remove the pull request from the queue, leaving the config."""
        self.redis.zrem(self._cache_key, pull_number)

    def remove_pull(self, pull_number):
        self._remove_pull(pull_number)
        self.redis.delete(self._config_cache_key(pull_number))
        self.redis.delete(self._old_config_cache_key(pull_number))
        self.log.info("pull request removed from merge queue", gh_pull=pull_number)

    def _move_pull_at_end(self, pull_number):  # pragma: no cover
        priority = self.get_config(pull_number)["effective_priority"]
        self._add_pull(pull_number, priority=priority, update=True)
        self.log.info(
            "pull request moved at the end of the merge queue", gh_pull=pull_number
        )

    def get_queue(self, ref):
        """Get a queue for another ref of this repository."""
        return self.__class__(
            self.redis,
            self.installation_id,
            self.owner,
            self.repo,
            ref,
        )

    def move_pull_to_new_base_branch(self, pull_number, new_queue):
        self._remove_pull(pull_number)
        priority = self.get_config(pull_number)["effective_priority"]
        new_queue._add_pull(pull_number, priority)
        self.log.info(
            "pull request moved from this queue to %s",
            new_queue,
            gh_pull=pull_number,
        )

    def is_first_pull(self, ctxt):
        pull_requests = self.get_pulls()
        if not pull_requests:
            ctxt.log.error("is_first_pull() called on empty queues")
            return True
        return pull_requests[0] == ctxt.pull["number"]

    def get_pulls(self):
        return [
            int(pull)
            for pull in self.redis.zrangebyscore(self._cache_key, "-inf", "+inf")
        ]

    def delete(self):
        self.redis.delete(self._cache_key)

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
            config = self.get_config(ctxt.pull["number"])
            conclusion, title, summary = helpers.update_pull_base_branch(
                ctxt,
                config["strict_method"],
                config["bot_account"],
            )

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
        with utils.get_redis_for_cache() as redis:
            LOG.info("smart strict workflow loop start")
            for queue_name in redis.keys("strict-merge-queues~*"):
                queue = cls.from_queue_name(redis, queue_name)
                try:
                    queue.process()
                except exceptions.MergifyNotInstalled:
                    queue.delete()
                except Exception:
                    queue.log.error("Fail to process merge queue", exc_info=True)
            LOG.info("smart strict workflow loop end")

    def process(self):
        pull_numbers = self.get_pulls()

        self.log.info("%d pulls queued", len(pull_numbers), queue=list(pull_numbers))

        if not pull_numbers:
            return

        pull_number = pull_numbers[0]

        with github.get_client(self.owner) as client:
            ctxt = None
            try:
                sub = asyncio.run(
                    subscription.Subscription.get_subscription(client.auth.owner_id)
                )
                data = client.item(
                    f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}"
                )

                ctxt = context.Context(client, data, sub)
                if ctxt.pull["base"]["ref"] != self.ref:
                    ctxt.log.info(
                        "pull request base branch have changed",
                        old_branch=self.ref,
                        new_branch=ctxt.pull["base"]["ref"],
                    )
                    self.move_pull_to_new_base_branch(
                        ctxt.pull["number"],
                        self.get_queue(ctxt.pull["base"]["ref"]),
                    )
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

            except Exception as exc:  # pragma: no cover
                log = self.log if ctxt is None else ctxt.log

                if exceptions.should_be_ignored(exc):
                    log.info(
                        "Fail to process merge queue, remove the pull request from the queue",
                        exc_info=True,
                    )
                    self.remove_pull(ctxt.pull["number"])

                elif exceptions.need_retry(exc):
                    log.info("Fail to process merge queue, need retry", exc_info=True)
                    if isinstance(exc, exceptions.MergeableStateUnknown):
                        # NOTE(sileht): We need GitHub to recompute the state here (by
                        # merging something else for example), so move it to the end
                        self._move_pull_at_end(pull_number)

                else:
                    log.error("Fail to process merge queue", exc_info=True)
                    self._move_pull_at_end(pull_number)
