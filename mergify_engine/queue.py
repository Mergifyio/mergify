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
import contextlib
import dataclasses
import json

import daiquiri
import redis

from mergify_engine import github_events
from mergify_engine import utils


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
    def _redis_queue_key(self):
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(self, ref):
        return (
            f"strict-merge-queues~{self.installation_id}~{self.owner}~{self.repo}~{ref}"
        )

    def _config_redis_queue_key(self, pull_number):
        return f"strict-merge-config~{self.installation_id}~{self.owner}~{self.repo}~{pull_number}"

    def get_config(self, pull_number: int) -> dict:
        """Return merge config for a pull request.

        Do not use it for logic, just for displaying the queue summary.

        :param pull_number: The pull request number.
        """
        config = self.redis.get(self._config_redis_queue_key(pull_number))
        if config is None:
            # TODO(sileht): Everything about queue should be done in redis transaction
            # e.g.: add/update/get/del of a pull in queue
            return {
                "strict_method": "merge",
                "priority": 2000,
                "effective_priority": 2000,
                "bot_account": None,
                "update_bot_account": None,
            }
        config = json.loads(config)
        # TODO(sileht): for compatibility purpose, we can drop that in a couple of week
        config.setdefault("effective_priority", config["priority"])
        config.setdefault("bot_account", None)
        config.setdefault("update_bot_account", None)
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
        with self.pull_request_auto_refresher(pull_number):
            return self.redis.zadd(self._redis_queue_key, {pull_number: score}, **flags)

    def add_pull(self, ctxt, config):
        self._remove_pull_from_other_queues(ctxt)

        self.redis.set(
            self._config_redis_queue_key(ctxt.pull["number"]),
            json.dumps(config),
        )
        added = self._add_pull(ctxt.pull["number"], config["effective_priority"])
        if added:
            self.log.info(
                "pull request added to merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )
        else:
            self.log.info(
                "pull request already in merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )

    def _remove_pull_from_other_queues(self, ctxt):
        # TODO(sileht): Find if there is an event when the base branch change to do this
        # only is this case.
        for queue_name in self.redis.keys(self._get_redis_queue_key_for("*")):
            if queue_name != self._redis_queue_key:
                oldqueue = Queue.from_queue_name(self.redis, queue_name)
                if ctxt.pull["number"] in oldqueue.get_pulls():
                    ctxt.log.info(
                        "pull request base branch have changed, cleaning old queue",
                        old_branch=oldqueue.ref,
                        new_branch=ctxt.pull["base"]["ref"],
                    )
                    oldqueue._remove_pull(ctxt.pull["number"])

    def _remove_pull(self, pull_number):
        """Remove the pull request from the queue, leaving the config."""
        with self.pull_request_auto_refresher(pull_number):
            self.redis.zrem(self._redis_queue_key, pull_number)

    def remove_pull(self, pull_number):
        self._remove_pull(pull_number)
        self.redis.delete(self._config_redis_queue_key(pull_number))
        self.log.info("pull request removed from merge queue", gh_pull=pull_number)

    def move_pull_at_end(self, pull_number, config):  # pragma: no cover
        priority = config["effective_priority"]
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

    def is_first_pull(self, ctxt):
        pull_requests = self.get_pulls()
        if not pull_requests:
            ctxt.log.error("is_first_pull() called on empty queues")
            return True
        return pull_requests[0] == ctxt.pull["number"]

    def get_pulls(self):
        return [
            int(pull)
            for pull in self.redis.zrangebyscore(self._redis_queue_key, "-inf", "+inf")
        ]

    def delete(self):
        self.redis.delete(self._redis_queue_key)

    @contextlib.contextmanager
    def pull_request_auto_refresher(self, pull_number):
        # NOTE(sileht): If the first queued pull request changes, we refresh it to sync
        # its base branch

        old_pull_requests = self.get_pulls()
        try:
            yield
        finally:
            if not old_pull_requests:
                return
            new_pull_requests = self.get_pulls()
            if not new_pull_requests:
                return

            if (
                new_pull_requests[0] != old_pull_requests[0]
                and new_pull_requests[0] != pull_number
            ):
                self.log.info(
                    "refreshing next pull in queue",
                    next_pull=new_pull_requests[0],
                    _from=old_pull_requests,
                    to=new_pull_requests,
                )
                asyncio.run(
                    github_events.send_refresh(
                        {
                            "number": new_pull_requests[0],
                            "base": {
                                "repo": {
                                    "name": self.repo,
                                    "owner": {"login": self.owner},
                                    "full_name": f"{self.owner}/{self.repo}",
                                }
                            },
                        }
                    )
                )
