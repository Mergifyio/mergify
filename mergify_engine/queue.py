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

from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import merge_train
from mergify_engine import rules
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)


class QueueConfig(typing.TypedDict):
    strict_method: typing.Literal["merge", "rebase", "squash"]
    priority: int
    effective_priority: int
    bot_account: typing.Optional[str]
    update_bot_account: typing.Optional[str]
    name: rules.QueueName


@dataclasses.dataclass
class Queue:
    repository: context.Repository
    ref: github_types.GitHubRefType
    train: typing.Optional[merge_train.Train]

    log: logging.LoggerAdapter = dataclasses.field(init=False)

    def __post_init__(self):
        self.log = daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.name,
            gh_branch=self.ref,
        )

    @classmethod
    async def from_context(cls, ctxt: context.Context, *, with_train: bool) -> "Queue":

        train: typing.Optional[merge_train.Train] = None
        if with_train:
            train = merge_train.Train(ctxt.repository, ctxt.pull["base"]["ref"])
            await train.load()

        return cls(ctxt.repository, ctxt.pull["base"]["ref"], train)

    async def get_queue_for(
        self, ref: github_types.GitHubRefType, with_train: bool
    ) -> "Queue":
        """Get a queue for another ref of this repository."""

        train: typing.Optional[merge_train.Train] = None
        if with_train:
            train = merge_train.Train(self.repository, ref)
            await train.load()

        return self.__class__(self.repository, ref, train)

    @property
    def _redis_queue_key(self) -> str:
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(
        self, ref: typing.Union[github_types.GitHubRefType, typing.Literal["*"]]
    ) -> str:
        return f"merge-queue~{self.repository.installation.owner_id}~{self.repository.id}~{ref}"

    def _config_redis_queue_key(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> str:
        return f"merge-config~{self.repository.installation.owner_id}~{self.repository.id}~{pull_number}"

    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> QueueConfig:
        """Return merge config for a pull request.

        Do not use it for logic, just for displaying the queue summary.

        :param pull_number: The pull request number.
        """
        config_str = await self.repository.installation.redis.get(
            self._config_redis_queue_key(pull_number)
        )
        if config_str is None:
            self.log.error(
                "pull request queued without associated configuration",
                gh_pull=pull_number,
            )
            return QueueConfig(
                {
                    "strict_method": "merge",
                    "priority": 2000,
                    "effective_priority": 2000,
                    "bot_account": None,
                    "update_bot_account": None,
                    "name": rules.QueueName(""),
                }
            )
        config: QueueConfig = json.loads(config_str)
        # TODO(sileht): for compatibility purpose, we can drop that in a couple of week
        config.setdefault("effective_priority", config["priority"])
        config.setdefault("bot_account", None)
        config.setdefault("update_bot_account", None)
        return config

    async def add_pull(self, ctxt: context.Context, config: QueueConfig) -> None:
        if self.train and ("name" not in config or config["name"] is None):
            raise RuntimeError("train without queue name")

        await self._remove_pull_from_other_queues(ctxt)

        async with await self.repository.installation.redis.pipeline() as pipeline:
            await pipeline.set(
                self._config_redis_queue_key(ctxt.pull["number"]),
                json.dumps(config),
            )

            score = utils.utcnow().timestamp() / config["effective_priority"]
            await pipeline.zaddoption(
                self._redis_queue_key, "NX", **{str(ctxt.pull["number"]): score}
            )
            added = (await pipeline.execute())[-1]

        if added:
            if self.train:
                position = await self.repository.installation.redis.zrank(
                    self._redis_queue_key, str(ctxt.pull["number"])
                )
                if position is None:
                    self.log.error(
                        "just added pull request already not in queue anymore",
                        gh_pull=ctxt.pull["number"],
                        config=config,
                    )
                else:
                    await self.train.insert_pull_at(ctxt, int(position), config["name"])

            self.log.info(
                "pull request added to merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )

            pull_requests_to_refresh = [
                p for p in await self.get_pulls() if p != ctxt.pull["number"]
            ]
            await self._refresh_pulls(pull_requests_to_refresh)
        else:
            if self.train:
                await self.train.refresh()

            self.log.info(
                "pull request already in merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )

    async def _remove_pull_from_other_queues(self, ctxt: context.Context) -> None:
        # TODO(sileht): Find if there is an event when the base branch change to do this
        # only is this case.
        for queue_name in await self.repository.installation.redis.keys(
            self._get_redis_queue_key_for("*")
        ):
            if queue_name != self._redis_queue_key:
                score = await self.repository.installation.redis.zscore(
                    queue_name, ctxt.pull["number"]
                )
                if score is not None:
                    old_branch = queue_name.split("~")[-1]
                    old_queue = await self.get_queue_for(
                        old_branch, with_train=bool(self.train)
                    )
                    ctxt.log.info(
                        "pull request base branch have changed, cleaning old queue",
                        old_branch=old_branch,
                        new_branch=ctxt.pull["base"]["ref"],
                    )
                    await old_queue.remove_pull(ctxt)

    async def remove_pull(
        self,
        ctxt: context.Context,
    ) -> None:

        async with await self.repository.installation.redis.pipeline() as pipeline:
            await pipeline.zrem(self._redis_queue_key, ctxt.pull["number"])
            await pipeline.delete(self._config_redis_queue_key(ctxt.pull["number"]))
            removed = (await pipeline.execute())[0]

        if removed > 0:
            if self.train:
                await self.train.remove_pull(ctxt)
            self.log.info(
                "pull request removed from merge queue", gh_pull=ctxt.pull["number"]
            )
            await self._refresh_pulls(await self.get_pulls())
        else:
            if self.train:
                await self.train.refresh()

            self.log.info(
                "pull request not in merge queue", gh_pull=ctxt.pull["number"]
            )

    async def is_first_pull(self, ctxt: context.Context) -> bool:
        pull_requests = await self.get_pulls()
        if not pull_requests:
            ctxt.log.error("is_first_pull() called on empty queues")
            return True
        return pull_requests[0] == ctxt.pull["number"]

    async def get_position(self, ctxt: context.Context) -> typing.Optional[int]:
        pulls = await self.get_pulls()
        try:
            return pulls.index(ctxt.pull["number"])
        except ValueError:
            return None

    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [
            github_types.GitHubPullRequestNumber(int(pull))
            for pull in await self.repository.installation.redis.zrangebyscore(
                self._redis_queue_key, "-inf", "+inf"
            )
        ]

    async def delete(self) -> None:
        await self.repository.installation.redis.delete(self._redis_queue_key)

    async def _refresh_pulls(
        self,
        pull_requests_to_refresh: typing.List[github_types.GitHubPullRequestNumber],
    ) -> None:
        async with utils.aredis_for_stream() as redis_stream:
            for pull in pull_requests_to_refresh:
                await github_events.send_refresh(
                    self.repository.installation.redis,
                    redis_stream,
                    {
                        "number": pull,
                        "base": {
                            "repo": {
                                "id": self.repository.id,
                                "name": self.repository.name,
                                "owner": {
                                    "login": self.repository.installation.owner_login,
                                    "id": self.repository.installation.owner_id,
                                },
                                "full_name": f"{self.repository.installation.owner_login}/{self.repository.name}",
                            }
                        },
                    },  # type: ignore
                )
