# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
import json
import typing

from mergify_engine import config
from mergify_engine import crypto
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine.clients import http


class ApplicationDashboardJSON(typing.TypedDict):
    id: int
    name: str
    github_account: github_types.GitHubAccount


class CachedApplication(typing.TypedDict):
    id: int
    name: str
    api_access_key: str
    api_secret_key: str
    account_id: github_types.GitHubAccountIdType


class ApplicationUserNotFound(Exception):
    pass


ApplicationClassT = typing.TypeVar("ApplicationClassT", bound="ApplicationBase")


@dataclasses.dataclass
class ApplicationBase:
    redis: utils.RedisCache
    id: int
    name: str
    api_access_key: str
    api_secret_key: str
    account_id: github_types.GitHubAccountIdType

    @classmethod
    async def delete(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
    ) -> None:
        raise NotImplementedError

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
    ) -> ApplicationClassT:
        raise NotImplementedError

    @classmethod
    async def update(
        cls, redis: utils.RedisCache, api_access_key: str, app: ApplicationDashboardJSON
    ) -> None:
        raise NotImplementedError


@dataclasses.dataclass
class ApplicationGitHubCom(ApplicationBase):
    ttl: int = -2

    RETENTION_SECONDS = 60 * 60 * 24 * 3  # 3 days
    VALIDITY_SECONDS = 3600

    @staticmethod
    def _cache_key(api_access_key: str) -> str:
        return f"api-key-cache~{api_access_key}"

    async def _has_expired(self) -> bool:
        if self.ttl < 0:  # not cached
            return True
        elapsed_since_stored = self.RETENTION_SECONDS - self.ttl
        return elapsed_since_stored > self.VALIDITY_SECONDS

    @classmethod
    async def delete(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
    ) -> None:
        await redis.delete(
            typing.cast(ApplicationGitHubCom, cls)._cache_key(api_access_key)
        )

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
    ) -> ApplicationClassT:
        return typing.cast(
            ApplicationClassT,
            await typing.cast(ApplicationGitHubCom, cls)._get(
                redis, api_access_key, api_secret_key
            ),
        )

    @classmethod
    async def _get(
        cls, redis: utils.RedisCache, api_access_key: str, api_secret_key: str
    ) -> "ApplicationGitHubCom":
        cached_application = await cls._retrieve_from_cache(
            redis, api_access_key, api_secret_key
        )
        if cached_application is None or await cached_application._has_expired():
            try:
                db_application = await cls._retrieve_from_db(
                    redis, api_access_key, api_secret_key
                )
            except http.HTTPNotFound:
                raise ApplicationUserNotFound()
            except Exception as exc:
                if cached_application is not None and (
                    exceptions.should_be_ignored(exc) or exceptions.need_retry(exc)
                ):
                    # NOTE(sileht): return the cached application, instead of
                    # retrying the stream, just because the dashboard has a
                    # connectivity issue.
                    return cached_application
                raise
            await db_application.save_to_cache()
            return db_application
        return cached_application

    async def save_to_cache(self) -> None:
        """Save an application to the cache."""
        await self.redis.setex(
            self._cache_key(self.api_access_key),
            self.RETENTION_SECONDS,
            crypto.encrypt(
                json.dumps(
                    CachedApplication(
                        {
                            "id": self.id,
                            "name": self.name,
                            "api_access_key": self.api_access_key,
                            "api_secret_key": self.api_secret_key,
                            "account_id": self.account_id,
                        }
                    )
                ).encode()
            ),
        )
        self.ttl = self.RETENTION_SECONDS

    @classmethod
    async def update(
        cls,
        redis: utils.RedisCache,
        api_access_key: str,
        data: ApplicationDashboardJSON,
    ) -> None:
        encrypted_application = await redis.get(cls._cache_key(api_access_key))
        if encrypted_application is not None:
            decrypted_application = typing.cast(
                CachedApplication,
                json.loads(crypto.decrypt(encrypted_application.encode()).decode()),
            )
            app = cls(
                redis,
                data["id"],
                data["name"],
                decrypted_application["api_access_key"],
                decrypted_application["api_secret_key"],
                data["github_account"]["id"],
            )
            await app.save_to_cache()
        return None

    @classmethod
    async def _retrieve_from_cache(
        cls, redis: utils.RedisCache, api_access_key: str, api_secret_key: str
    ) -> typing.Optional["ApplicationGitHubCom"]:
        async with await redis.pipeline() as pipe:
            await pipe.get(cls._cache_key(api_access_key))
            await pipe.ttl(cls._cache_key(api_access_key))
            encrypted_application, ttl = typing.cast(
                typing.Tuple[str, int], await pipe.execute()
            )
        if encrypted_application:
            decrypted_application = typing.cast(
                CachedApplication,
                json.loads(crypto.decrypt(encrypted_application.encode()).decode()),
            )
            if decrypted_application["api_secret_key"] != api_secret_key:
                # Don't raise ApplicationUserNotFound yet, check the database first
                return None

            if "id" not in decrypted_application:
                # TODO(sileht): Backward compat, delete me
                return None

            return cls(
                redis,
                decrypted_application["id"],
                decrypted_application["name"],
                decrypted_application["api_access_key"],
                decrypted_application["api_secret_key"],
                decrypted_application["account_id"],
                ttl,
            )
        return None

    @classmethod
    async def _retrieve_from_db(
        cls, redis: utils.RedisCache, api_access_key: str, api_secret_key: str
    ) -> "ApplicationGitHubCom":
        async with http.AsyncClient() as client:
            resp = await client.get(
                f"{config.SUBSCRIPTION_BASE_URL}/engine/applications/{api_access_key}{api_secret_key}",
                auth=(config.OAUTH_CLIENT_ID, config.OAUTH_CLIENT_SECRET),
            )
            data = typing.cast(ApplicationDashboardJSON, resp.json())
            return cls(
                redis,
                data["id"],
                data["name"],
                api_access_key,
                api_secret_key,
                data["github_account"]["id"],
            )


@dataclasses.dataclass
class ApplicationOnPremise(ApplicationBase):
    @classmethod
    async def delete(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
    ) -> None:
        pass

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
    ) -> ApplicationClassT:
        data = config.APPLICATION_APIKEYS.get(api_access_key)
        if data is None or data["api_secret_key"] != api_secret_key:
            raise ApplicationUserNotFound()
        return cls(
            redis,
            0,
            "on-premise-app",
            api_access_key,
            api_secret_key,
            github_types.GitHubAccountIdType(data["account_id"]),
        )


if config.SUBSCRIPTION_TOKEN is not None:

    @dataclasses.dataclass
    class Application(ApplicationOnPremise):
        pass


else:

    @dataclasses.dataclass
    class Application(ApplicationGitHubCom):  # type: ignore [no-redef]
        pass
