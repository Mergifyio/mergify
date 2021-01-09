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
import logging
import typing

import daiquiri
import redis

from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)


class QueueConfig(typing.TypedDict):
    strict_method: typing.Literal["merge", "rebase", "squash"]
    priority: int
    effective_priority: int
    bot_account: typing.Optional[str]
    update_bot_account: typing.Optional[str]


@dataclasses.dataclass
class Queue:
    redis: redis.Redis
    owner_id: int
    owner: str
    repo_id: int
    repo: str
    ref: str

    log: logging.LoggerAdapter = dataclasses.field(init=False)

    def __post_init__(self):
        self.log = daiquiri.getLogger(
            __name__, gh_owner=self.owner, gh_repo=self.repo, gh_branch=self.ref
        )

    @classmethod
    def from_context(cls, ctxt: context.Context) -> "Queue":
        return cls(
            utils.get_redis_for_cache(),
            ctxt.pull["base"]["repo"]["owner"]["id"],
            ctxt.pull["base"]["repo"]["owner"]["login"],
            ctxt.pull["base"]["repo"]["id"],
            ctxt.pull["base"]["repo"]["name"],
            ctxt.pull["base"]["ref"],
        )

    @property
    def _redis_queue_key(self) -> str:
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(self, ref: str) -> str:
        return f"merge-queue~{self.owner_id}~{self.repo_id}~{ref}"

    def _config_redis_queue_key(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> str:
        return f"merge-config~{self.owner_id}~{self.repo_id}~{pull_number}"

    def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> QueueConfig:
        """Return merge config for a pull request.

        Do not use it for logic, just for displaying the queue summary.

        :param pull_number: The pull request number.
        """
        config_str = self.redis.get(self._config_redis_queue_key(pull_number))
        if config_str is None:
            # TODO(sileht): Everything about queue should be done in redis transaction
            # e.g.: add/update/get/del of a pull in queue
            return QueueConfig(
                {
                    "strict_method": "merge",
                    "priority": 2000,
                    "effective_priority": 2000,
                    "bot_account": None,
                    "update_bot_account": None,
                }
            )
        config: QueueConfig = json.loads(config_str)
        # TODO(sileht): for compatibility purpose, we can drop that in a couple of week
        config.setdefault("effective_priority", config["priority"])
        config.setdefault("bot_account", None)
        config.setdefault("update_bot_account", None)
        return config

    def add_pull(self, ctxt: context.Context, config: QueueConfig) -> None:
        self._remove_pull_from_other_queues(ctxt)

        self.redis.set(
            self._config_redis_queue_key(ctxt.pull["number"]),
            json.dumps(config),
        )

        score = utils.utcnow().timestamp() / config["effective_priority"]
        added = self.redis.zadd(
            self._redis_queue_key, {str(ctxt.pull["number"]): score}, nx=True
        )

        if added:
            self.log.info(
                "pull request added to merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )
            pull_requests_to_refresh = [
                p for p in self.get_pulls() if p != ctxt.pull["number"]
            ]
            self._refresh_pulls(pull_requests_to_refresh)
        else:
            self.log.info(
                "pull request already in merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )

    def _remove_pull_from_other_queues(self, ctxt: context.Context) -> None:
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
                    old_queue.remove_pull(ctxt)

    def remove_pull(self, ctxt: context.Context) -> None:
        removed = self.redis.zrem(self._redis_queue_key, ctxt.pull["number"])
        if removed > 0:
            self.redis.delete(self._config_redis_queue_key(ctxt.pull["number"]))
            self.log.info(
                "pull request removed from merge queue", gh_pull=ctxt.pull["number"]
            )
            self._refresh_pulls(self.get_pulls())
        else:
            self.log.info(
                "pull request not in merge queue", gh_pull=ctxt.pull["number"]
            )

    def get_queue(self, ref: str) -> "Queue":
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

    def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [
            typing.cast(github_types.GitHubPullRequestNumber, int(pull))
            for pull in self.redis.zrangebyscore(self._redis_queue_key, "-inf", "+inf")
        ]

    def delete(self) -> None:
        self.redis.delete(self._redis_queue_key)

    def _refresh_pulls(
        self,
        pull_requests_to_refresh: typing.List[github_types.GitHubPullRequestNumber],
    ) -> None:
        for pull in pull_requests_to_refresh:
            utils.async_run(
                github_events.send_refresh(
                    {
                        "number": pull,
                        "base": {
                            "repo": {
                                "id": self.repo_id,
                                "name": self.repo,
                                "owner": {"login": self.owner, "id": self.owner_id},
                                "full_name": f"{self.owner}/{self.repo}",
                            }
                        },
                    }  # type: ignore
                )
            )
