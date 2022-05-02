# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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
import datetime
import typing

import daiquiri
import msgpack

from mergify_engine import context
from mergify_engine import date
from mergify_engine.queue import merge_train


LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class QueueFreeze:

    repository: context.Repository

    # Stored in redis
    name: str = dataclasses.field(metadata={"description": "Queue name"})
    application_name: str = dataclasses.field(
        metadata={"description": "Application name responsible for the freeze"},
    )
    application_id: int = dataclasses.field(
        metadata={"description": "Application ID responsible for the freeze"},
    )
    reason: str = dataclasses.field(
        default_factory=str, metadata={"description": "Freeze reason"}
    )
    freeze_date: datetime.datetime = dataclasses.field(
        default_factory=date.utcnow,
        metadata={"description": "The date and time of the freeze"},
    )

    class Serialized(typing.TypedDict):
        name: str
        reason: str
        application_name: str
        application_id: str
        freeze_date: datetime.datetime

    @classmethod
    async def get_all(
        cls, repository: context.Repository
    ) -> typing.AsyncGenerator["QueueFreeze", None]:

        async for key, qf_raw in repository.installation.redis.queue.hscan_iter(
            name=cls._get_redis_hash(repository),
            match=cls._get_redis_key_match(repository),
        ):
            qf = msgpack.unpackb(qf_raw, timestamp=3)
            yield cls(
                repository=repository,
                name=qf["name"],
                reason=qf["reason"],
                application_name=qf["application_name"],
                application_id=qf["application_id"],
                freeze_date=qf["freeze_date"],
            )

    @classmethod
    async def get(
        cls, repository: context.Repository, queue_name: str
    ) -> typing.Optional["QueueFreeze"]:

        qf_raw = await repository.installation.redis.queue.hget(
            cls._get_redis_hash(repository),
            cls._get_redis_key(repository, queue_name),
        )

        if qf_raw is None:
            return None

        # NOTE(Syffe): timestamp parameter means that timestamp variables will be converted to
        # datetime (value 3=to_datetime()). Other values can be used: 1=to_float(), 2=to_unix_ns()
        qf = msgpack.unpackb(qf_raw, timestamp=3)

        return cls(
            repository=repository,
            name=qf["name"],
            reason=qf["reason"],
            application_name=qf["application_name"],
            application_id=qf["application_id"],
            freeze_date=qf["freeze_date"],
        )

    @classmethod
    def _get_redis_hash(cls, repository: context.Repository) -> str:
        return f"merge-freeze~{repository.installation.owner_id}"

    @classmethod
    def _get_redis_key(cls, repository: context.Repository, queue_name: str) -> str:
        return f"{repository.repo['id']}~{queue_name}"

    @classmethod
    def _get_redis_key_match(cls, repository: context.Repository) -> str:
        return f"{repository.repo['id']}~*"

    async def save(self) -> None:

        await self.repository.installation.redis.queue.hset(
            self._get_redis_hash(self.repository),
            self._get_redis_key(self.repository, self.name),
            msgpack.packb(
                {
                    "name": self.name,
                    "reason": self.reason,
                    "application_name": self.application_name,
                    "application_id": self.application_id,
                    "freeze_date": self.freeze_date,
                },
                # NOTE(Syffe): datetime parameter means that datetime variables will be converted to a timestamp
                # in order to be serialized
                datetime=True,
            ),
        )

        await self._refresh_pulls(source="internal/queue_freeze_create")

    async def delete(self) -> bool:

        result = bool(
            await self.repository.installation.redis.queue.hdel(
                self._get_redis_hash(self.repository),
                self._get_redis_key(self.repository, self.name),
            )
        )

        await self._refresh_pulls(source="internal/queue_freeze_delete")

        return result

    async def _refresh_pulls(self, source: str) -> None:
        async for train in merge_train.Train.iter_trains(self.repository):
            await train.refresh_pulls(source=source)
