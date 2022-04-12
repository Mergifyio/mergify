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
import abc
import dataclasses
import enum
import functools
import typing

import daiquiri
import voluptuous

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import utils


if typing.TYPE_CHECKING:
    from mergify_engine import rules

LOG = daiquiri.getLogger(__name__)


class PriorityAliases(enum.Enum):
    low = 1000
    medium = 2000
    high = 3000


def Priority(v):
    try:
        return PriorityAliases[v].value
    except KeyError:
        return v


MAX_PRIORITY: int = 10000
# NOTE(sileht): We use the max priority as an offset to order queue
QUEUE_PRIORITY_OFFSET: int = MAX_PRIORITY

PrioritySchema = voluptuous.All(
    voluptuous.Any("low", "medium", "high", int),
    voluptuous.Coerce(Priority),
    int,
    voluptuous.Range(min=1, max=MAX_PRIORITY),
)


class PullQueueConfig(typing.TypedDict):
    strict_method: typing.Literal["merge", "rebase"]
    update_method: typing.Literal["merge", "rebase"]
    priority: int
    effective_priority: int
    bot_account: typing.Optional[github_types.GitHubLogin]
    update_bot_account: typing.Optional[github_types.GitHubLogin]
    name: "rules.QueueName"
    queue_config: "rules.QueueConfig"


QueueT = typing.TypeVar("QueueT", bound="QueueBase")


@dataclasses.dataclass  # type: ignore
class QueueBase(abc.ABC):
    repository: context.Repository
    ref: github_types.GitHubRefType

    @functools.cached_property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.repo["name"],
            gh_branch=self.ref,
        )

    @classmethod
    async def from_context(cls: typing.Type[QueueT], ctxt: context.Context) -> QueueT:
        return cls(ctxt.repository, ctxt.pull["base"]["ref"])

    @abc.abstractmethod
    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> PullQueueConfig:
        """Return merge config for a pull request.

        Do not use it for logic, just for displaying the queue summary.

        :param pull_number: The pull request number.
        """

    @abc.abstractmethod
    async def add_pull(self, ctxt: context.Context, config: PullQueueConfig) -> None:
        pass

    @abc.abstractmethod
    async def remove_pull(self, ctxt: context.Context) -> None:
        pass

    @abc.abstractmethod
    async def is_first_pull(self, ctxt: context.Context) -> bool:
        pass

    @abc.abstractmethod
    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        """Return ordered queued pull requests"""
        pass

    async def get_position(self, ctxt: context.Context) -> typing.Optional[int]:
        pulls = await self.get_pulls()
        try:
            return pulls.index(ctxt.pull["number"])
        except ValueError:
            return None

    async def refresh_pulls(
        self,
        source: str,
        additional_pull_request: typing.Optional[
            github_types.GitHubPullRequestNumber
        ] = None,
    ) -> None:

        pulls = set(await self.get_pulls())
        if additional_pull_request:
            pulls.add(additional_pull_request)

        with utils.yaaredis_for_stream() as redis_stream:
            pipe = await redis_stream.pipeline()
            for pull_number in pulls:
                await utils.send_pull_refresh(
                    self.repository.installation.redis,
                    pipe,
                    self.repository.repo,
                    pull_request_number=pull_number,
                    action="internal",
                    source=source,
                )
            await pipe.execute()
