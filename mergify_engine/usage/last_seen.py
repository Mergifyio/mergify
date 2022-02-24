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

from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import signals
from mergify_engine import utils


# Keep data 60 days after last signal
RETENTION_SECONDS = int(datetime.timedelta(days=60).total_seconds())


def get_last_seen_key(account_id: github_types.GitHubAccountIdType) -> str:
    return f"usage/last_seen/{account_id}"


async def update(
    ctxt: context.Context,
    event: signals.EventName,
    metadata: typing.Optional[signals.SignalMetadata],
) -> None:
    key = get_last_seen_key(ctxt.repository.installation.owner_id)
    now = date.utcnow().isoformat()
    await ctxt.redis.setex(key, RETENTION_SECONDS, now)


async def get(
    redis: utils.RedisCache,
    owner_id: github_types.GitHubAccountIdType,
) -> typing.Optional[datetime.datetime]:
    raw = typing.cast(
        typing.Optional[str],
        await redis.get(get_last_seen_key(owner_id)),
    )
    if raw is None:
        return None
    else:
        return date.fromisoformat(raw)
