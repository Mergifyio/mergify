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
import functools
import typing

import daiquiri

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)


class PullQueueConfig(typing.TypedDict):
    strict_method: typing.Literal["merge", "rebase"]
    priority: int
    effective_priority: int
    bot_account: typing.Optional[str]
    update_bot_account: typing.Optional[str]
    name: rules.QueueName
    queue_config: rules.QueueConfig


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

    async def _refresh_pulls(
        self,
        repository: github_types.GitHubRepository,
        except_pull_request: typing.Optional[
            github_types.GitHubPullRequestNumber
        ] = None,
    ) -> None:

        with utils.aredis_for_stream() as redis_stream:
            for pull_number in await self.get_pulls():
                if (
                    except_pull_request is not None
                    and except_pull_request == pull_number
                ):
                    continue
                await utils.send_refresh(
                    self.repository.installation.redis,
                    redis_stream,
                    repository,
                    pull_request_number=pull_number,
                    action="internal",
                )
