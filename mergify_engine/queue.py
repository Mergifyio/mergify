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
    repository: context.Repository
    ref: str

    log: logging.LoggerAdapter = dataclasses.field(init=False)

    def __post_init__(self):
        self.log = daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.name,
            gh_branch=self.ref,
        )

    @classmethod
    def from_context(cls, ctxt: context.Context) -> "Queue":
        return cls(ctxt.repository, ctxt.pull["base"]["ref"])

    @property
    def _redis_queue_key(self) -> str:
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(self, ref: str) -> str:
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
                }
            )
        config: QueueConfig = json.loads(config_str)
        # TODO(sileht): for compatibility purpose, we can drop that in a couple of week
        config.setdefault("effective_priority", config["priority"])
        config.setdefault("bot_account", None)
        config.setdefault("update_bot_account", None)
        return config

    async def add_pull(self, ctxt: context.Context, config: QueueConfig) -> None:
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
                    old_queue = self.get_queue(old_branch)
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
            self.log.info(
                "pull request removed from merge queue", gh_pull=ctxt.pull["number"]
            )
            await self._refresh_pulls(await self.get_pulls())
        else:
            self.log.info(
                "pull request not in merge queue", gh_pull=ctxt.pull["number"]
            )

    def get_queue(self, ref: str) -> "Queue":
        """Get a queue for another ref of this repository."""
        return self.__class__(self.repository, ref)

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
