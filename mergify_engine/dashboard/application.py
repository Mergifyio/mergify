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
from mergify_engine import redis_utils
from mergify_engine.clients import dashboard
from mergify_engine.clients import http


class ApplicationAccountScope(typing.TypedDict):
    id: github_types.GitHubAccountIdType
    login: github_types.GitHubLogin


class ApplicationDashboardJSON(typing.TypedDict):
    id: int
    name: str
    github_account: github_types.GitHubAccount


class CachedApplication(typing.TypedDict):
    id: int
    name: str
    api_access_key: str
    api_secret_key: str
    account_scope: typing.Optional[ApplicationAccountScope]


class ApplicationUserNotFound(Exception):
    pass


ApplicationClassT = typing.TypeVar("ApplicationClassT", bound="ApplicationBase")


@dataclasses.dataclass
class ApplicationBase:
    redis: redis_utils.RedisCache
    id: int
    name: str
    api_access_key: str
    api_secret_key: str
    account_scope: typing.Optional[ApplicationAccountScope]

    @classmethod
    async def delete(cls, redis: redis_utils.RedisCache, api_access_key: str) -> None:
        raise NotImplementedError

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> ApplicationClassT:
        raise NotImplementedError

    @classmethod
    async def update(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
        app: ApplicationDashboardJSON,
    ) -> None:
        raise NotImplementedError


@dataclasses.dataclass
class ApplicationSaas(ApplicationBase):
    ttl: int = -2

    RETENTION_SECONDS = 60 * 60 * 24 * 3  # 3 days
    VALIDITY_SECONDS = 3600

    @staticmethod
    def _cache_key(
        api_access_key: str,
        account_scope: typing.Union[
            typing.Literal["*"], typing.Optional[github_types.GitHubLogin]
        ],
    ) -> str:
        if account_scope is None:
            account_scope_key = "#"
        else:
            account_scope_key = account_scope.lower()
        return f"api-key-cache~{api_access_key}~{account_scope_key}"

    def _has_expired(self) -> bool:
        if self.ttl < 0:  # not cached
            return True
        elapsed_since_stored = self.RETENTION_SECONDS - self.ttl
        return elapsed_since_stored > self.VALIDITY_SECONDS

    @classmethod
    async def delete(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
    ) -> None:
        pipe = await redis.pipeline()
        async for key in redis.scan_iter(
            match=cls._cache_key(api_access_key, "*"), count=10000
        ):
            await pipe.delete(key)
        await pipe.execute()

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> ApplicationClassT:
        return typing.cast(
            ApplicationClassT,
            await typing.cast(ApplicationSaas, cls)._get(
                redis, api_access_key, api_secret_key, account_scope
            ),
        )

    @classmethod
    async def _get(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> "ApplicationSaas":
        cached_application = await cls._retrieve_from_cache(
            redis, api_access_key, api_secret_key, account_scope
        )
        if cached_application is None or cached_application._has_expired():
            try:
                db_application = await cls._retrieve_from_db(
                    redis, api_access_key, api_secret_key, account_scope
                )
            except http.HTTPForbidden:
                # api key is valid, but not the scope
                raise ApplicationUserNotFound()
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
            self._cache_key(
                self.api_access_key,
                None if self.account_scope is None else self.account_scope["login"],
            ),
            self.RETENTION_SECONDS,
            crypto.encrypt(
                json.dumps(
                    CachedApplication(
                        {
                            "id": self.id,
                            "name": self.name,
                            "api_access_key": self.api_access_key,
                            "api_secret_key": self.api_secret_key,
                            "account_scope": self.account_scope,
                        }
                    )
                ).encode()
            ),
        )
        self.ttl = self.RETENTION_SECONDS

    @classmethod
    async def update(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
        data: ApplicationDashboardJSON,
    ) -> None:
        if data["github_account"] is None:
            account_scope = None
        else:
            account_scope = data["github_account"]["login"]

        encrypted_application = await redis.get(
            cls._cache_key(api_access_key, account_scope)
        )
        if encrypted_application is not None:
            decrypted_application = typing.cast(
                CachedApplication,
                json.loads(crypto.decrypt(encrypted_application.encode()).decode()),
            )
            if "account_scope" not in decrypted_application:
                # TODO(sileht): Backward compat, delete me
                return None

            if data["github_account"] is None:
                full_account_scope = None
            else:
                full_account_scope = ApplicationAccountScope(
                    {
                        "id": data["github_account"]["id"],
                        "login": data["github_account"]["login"],
                    }
                )
            app = cls(
                redis,
                data["id"],
                data["name"],
                decrypted_application["api_access_key"],
                decrypted_application["api_secret_key"],
                full_account_scope,
            )
            await app.save_to_cache()
        return None

    @classmethod
    async def _retrieve_from_cache(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> typing.Optional["ApplicationSaas"]:
        async with await redis.pipeline() as pipe:
            await pipe.get(cls._cache_key(api_access_key, account_scope))
            await pipe.ttl(cls._cache_key(api_access_key, account_scope))
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

            if "account_scope" not in decrypted_application:
                # TODO(sileht): Backward compat, delete me
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
                decrypted_application["account_scope"],
                ttl,
            )
        return None

    @classmethod
    async def _retrieve_from_db(
        cls,
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> "ApplicationSaas":
        async with dashboard.AsyncDashboardSaasClient() as client:
            headers: typing.Dict[str, str]
            if account_scope is None:
                headers = {}
            else:
                headers = {"Mergify-Application-Account-Scope": account_scope}
            resp = await client.post(
                "/engine/applications",
                json={"token": f"{api_access_key}{api_secret_key}"},
                headers=headers,
            )
            data = typing.cast(ApplicationDashboardJSON, resp.json())
            return cls(
                redis,
                data["id"],
                data["name"],
                api_access_key,
                api_secret_key,
                account_scope=None
                if account_scope is None
                else ApplicationAccountScope(
                    {
                        "id": data["github_account"]["id"],
                        "login": data["github_account"]["login"],
                    }
                ),
            )


@dataclasses.dataclass
class ApplicationOnPremise(ApplicationBase):
    @classmethod
    async def delete(cls, redis: redis_utils.RedisCache, api_access_key: str) -> None:
        pass

    @classmethod
    async def get(
        cls: typing.Type[ApplicationClassT],
        redis: redis_utils.RedisCache,
        api_access_key: str,
        api_secret_key: str,
        account_scope: typing.Optional[github_types.GitHubLogin],
    ) -> ApplicationClassT:
        data = config.APPLICATION_APIKEYS.get(api_access_key)
        if data is None or data["api_secret_key"] != api_secret_key:
            raise ApplicationUserNotFound()
        if account_scope is not None and account_scope != data["account_login"]:
            raise ApplicationUserNotFound()
        return cls(
            redis,
            0,
            "on-premise-app",
            api_access_key,
            api_secret_key,
            ApplicationAccountScope(
                {
                    "id": github_types.GitHubAccountIdType(data["account_id"]),
                    "login": github_types.GitHubLogin(data["account_login"]),
                }
            ),
        )


if config.SAAS_MODE:

    @dataclasses.dataclass
    class Application(ApplicationSaas):
        pass

else:

    @dataclasses.dataclass
    class Application(ApplicationOnPremise):  # type: ignore [no-redef]
        pass
