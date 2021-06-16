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
import typing

import daiquiri

from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import queue
from mergify_engine import rules


LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class Queue(queue.QueueBase):
    async def _get_queue_for(self, ref: github_types.GitHubRefType) -> "Queue":
        """Get a queue for another ref of this repository."""
        return self.__class__(self.repository, ref)

    async def load(self) -> None:
        pass

    @property
    def _redis_queue_key(self) -> str:
        return self._get_redis_queue_key_for(self.ref)

    def _get_redis_queue_key_for(
        self, ref: typing.Union[github_types.GitHubRefType, typing.Literal["*"]]
    ) -> str:
        return f"merge-queue~{self.repository.installation.owner_id}~{self.repository.repo['id']}~{ref}"

    def _config_redis_queue_key(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> str:
        return f"merge-config~{self.repository.installation.owner_id}~{self.repository.repo['id']}~{pull_number}"

    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> queue.PullQueueConfig:
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
            return queue.PullQueueConfig(
                {
                    "strict_method": "merge",
                    "priority": 2000,
                    "effective_priority": 2000,
                    "bot_account": None,
                    "update_bot_account": None,
                    "name": rules.QueueName(""),
                    "queue_config": rules.QueueConfig(
                        {"priority": 1, "speculative_checks": 1}
                    ),
                }
            )
        config: queue.PullQueueConfig = json.loads(config_str)
        return config

    async def add_pull(
        self, ctxt: context.Context, config: queue.PullQueueConfig
    ) -> None:
        await self._remove_pull_from_other_queues(ctxt)

        async with await self.repository.installation.redis.pipeline() as pipeline:
            await pipeline.set(
                self._config_redis_queue_key(ctxt.pull["number"]),
                json.dumps(config),
            )

            score = date.utcnow().timestamp() / config["effective_priority"]
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

            await self._refresh_pulls(
                ctxt.pull["base"]["repo"], except_pull_request=ctxt.pull["number"]
            )
        else:
            self.log.info(
                "pull request already in merge queue",
                gh_pull=ctxt.pull["number"],
                config=config,
            )

    async def _remove_pull_from_other_queues(self, ctxt: context.Context) -> None:
        # TODO(sileht): Find if there is an event when the base branch change to do this
        # only is this case.
        async for queue_name in self.repository.installation.redis.scan_iter(
            self._get_redis_queue_key_for("*"), count=10000
        ):
            if queue_name != self._redis_queue_key:
                score = await self.repository.installation.redis.zscore(
                    queue_name, ctxt.pull["number"]
                )
                if score is not None:
                    old_branch = queue_name.split("~")[-1]
                    old_queue = await self._get_queue_for(old_branch)
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
            await self._refresh_pulls(ctxt.pull["base"]["repo"])
        else:
            self.log.info(
                "pull request not in merge queue", gh_pull=ctxt.pull["number"]
            )

    async def is_first_pull(self, ctxt: context.Context) -> bool:
        pull_requests = await self.get_pulls()
        if not pull_requests:
            ctxt.log.error("is_first_pull() called on empty queues")
            return True
        return pull_requests[0] == ctxt.pull["number"]

    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [
            github_types.GitHubPullRequestNumber(int(pull))
            for pull in await self.repository.installation.redis.zrangebyscore(
                self._redis_queue_key, "-inf", "+inf"
            )
        ]
