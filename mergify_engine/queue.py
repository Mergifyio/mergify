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
import contextlib
import dataclasses
import json
import typing

import daiquiri
import redis

from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class Queue:
    redis: redis.Redis
    owner_id: int
    owner: str
    repo_id: int
    repo: str
    ref: str

    def __post_init__(self):
        # TODO(sileht): Remove me when no more old keys are present
        for old_queue_key in self.redis.keys(
            f"strict-merge-queues~*~{self.owner}~{self.repo}~*"
        ):
            _, installation_id, _, _, ref = old_queue_key.split("~")
            new_queue_key = f"merge-queue~{self.owner_id}~{self.repo_id}~{ref}"

            for old_config_key in self.redis.keys(
                f"strict-merge-config~{installation_id}~{self.owner}~{self.repo}~*"
            ):
                pull_number = old_config_key.split("~")[-1]
                new_config_key = (
                    f"merge-config~{self.owner_id}~{self.repo_id}~{pull_number}"
                )
                self.redis.rename(old_config_key, new_config_key)

            self.redis.rename(old_queue_key, new_queue_key)

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__, gh_owner=self.owner, gh_repo=self.repo, gh_branch=self.ref
        )

    @classmethod
    def from_context(cls, ctxt):
        return cls(
            utils.get_redis_for_cache(),
            ctxt.pull["base"]["repo"]["owner"]["id"],
            ctxt.pull["base"]["repo"]["owner"]["login"],
            ctxt.pull["base"]["repo"]["id"],
            ctxt.pull["base"]["repo"]["name"],
            ctxt.pull["base"]["ref"],
        )

    @property
    def _redis_queue_key(self):
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(self, ref):
        return f"merge-queue~{self.owner_id}~{self.repo_id}~{ref}"

    def _config_redis_queue_key(self, pull_number):
        return f"merge-config~{self.owner_id}~{self.repo_id}~{pull_number}"

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
                score = self.redis.zscore(queue_name, ctxt.pull["number"])
                if score is not None:
                    old_branch = queue_name.split("~")[-1]
                    old_queue = self.get_queue(old_branch)
                    ctxt.log.info(
                        "pull request base branch have changed, cleaning old queue",
                        old_branch=old_branch,
                        new_branch=ctxt.pull["base"]["ref"],
                    )
                    old_queue._remove_pull(ctxt.pull["number"])

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
            self.owner_id,
            self.owner,
            self.repo_id,
            self.repo,
            ref,
        )

    def is_first_pull(self, ctxt: context.Context) -> bool:
        pull_requests = self.get_pulls()
        if not pull_requests:
            ctxt.log.error("is_first_pull() called on empty queues")
            return True
        return pull_requests[0] == ctxt.pull["number"]

    def get_position(self, ctxt: context.Context) -> typing.Optional[int]:
        pulls = self.get_pulls()
        try:
            return pulls.index(ctxt.pull["number"])
        except ValueError:
            return None

    def get_pulls(self):
        return [
            int(pull)
            for pull in self.redis.zrangebyscore(self._redis_queue_key, "-inf", "+inf")
        ]

    def delete(self):
        self.redis.delete(self._redis_queue_key)

    @contextlib.contextmanager
    def pull_request_auto_refresher(self, pull_number):
        # NOTE(sileht): we need to refresh PR for two reasons:
        # * sync the first one from its base branch if needed
        # * update all summary with the new queues

        old_pull_requests = self.get_pulls()
        try:
            yield
        finally:
            new_pull_requests = self.get_pulls()
            if new_pull_requests == old_pull_requests:
                return

            pull_requests_to_refresh = [
                p for p in new_pull_requests if p != pull_number
            ]
            if not pull_requests_to_refresh:
                return

            self.log.info(
                "queue changed, refreshing all pull requests",
                _from=old_pull_requests,
                _to=new_pull_requests,
                _to_refresh=pull_requests_to_refresh,
            )

            for pull in pull_requests_to_refresh:
                utils.async_run(
                    github_events.send_refresh(
                        {
                            "number": pull,
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
