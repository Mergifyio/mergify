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

import datetime
import typing

from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import signals


if typing.TYPE_CHECKING:
    from mergify_engine import context
    from mergify_engine import redis_utils

# Keep data 60 days after last signal
RETENTION_SECONDS = int(datetime.timedelta(days=60).total_seconds())


def get_last_seen_key(account_id: github_types.GitHubAccountIdType) -> str:
    return f"usage/last_seen/{account_id}"


class Signal(signals.SignalBase):
    async def __call__(
        self,
        repository: "context.Repository",
        pull_request: github_types.GitHubPullRequestNumber,
        event: signals.EventName,
        metadata: signals.EventMetadata,
        trigger: str,
    ) -> None:
        key = get_last_seen_key(repository.installation.owner_id)
        now = date.utcnow().isoformat()
        await repository.installation.redis.cache.setex(key, RETENTION_SECONDS, now)


async def get(
    redis: "redis_utils.RedisCache",
    owner_id: github_types.GitHubAccountIdType,
) -> typing.Optional[datetime.datetime]:
    raw = await redis.get(get_last_seen_key(owner_id))
    if raw is None:
        return None
    else:
        return date.fromisoformat(raw)
