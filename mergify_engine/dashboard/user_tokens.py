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

import daiquiri

from mergify_engine import config
from mergify_engine import crypto
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.clients import dashboard
from mergify_engine.clients import http


if typing.TYPE_CHECKING:
    from mergify_engine import context

LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class UserTokensUserNotFound(Exception):
    reason: str


class UserTokensUser(typing.TypedDict):
    id: github_types.GitHubAccountIdType
    login: github_types.GitHubLogin
    oauth_access_token: github_types.GitHubOAuthToken
    name: typing.Optional[str]
    email: typing.Optional[str]


UserTokensT = typing.TypeVar("UserTokensT", bound="UserTokensBase")


@dataclasses.dataclass
class UserTokensBase:
    redis: redis_utils.RedisCache
    owner_id: int
    users: typing.List[UserTokensUser]

    @staticmethod
    async def select_users_for(
        ctxt: "context.Context",
        bot_account: typing.Optional[github_types.GitHubLogin] = None,
    ) -> typing.List[UserTokensUser]:
        user_tokens = await ctxt.repository.installation.get_user_tokens()
        if bot_account:
            user = user_tokens.get_token_for(bot_account)
            if user:
                users = [user]
            else:
                raise UserTokensUserNotFound(
                    f"User `{bot_account}` is unknown. Please make sure `{bot_account}` has logged in Mergify dashboard."
                )
        else:
            users = user_tokens.users

        # Pick author first
        users = sorted(users, key=lambda x: x["login"] != ctxt.pull["user"]["login"])
        return users

    def get_token_for(
        self, wanted_login: github_types.GitHubLogin
    ) -> typing.Optional[UserTokensUser]:
        wanted_login_lower = wanted_login.lower()
        for user in self.users:
            if user["login"].lower() == wanted_login_lower:
                return user
        return None

    @classmethod
    async def delete(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> None:
        raise NotImplementedError

    @classmethod
    async def get(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> UserTokensT:
        raise NotImplementedError


@dataclasses.dataclass
class UserTokensSaas(UserTokensBase):
    ttl: int = -2

    RETENTION_SECONDS = 60 * 60 * 24 * 3  # 3 days
    VALIDITY_SECONDS = 3600

    @staticmethod
    def _cache_key(owner_id: int) -> str:
        return f"user-tokens-cache-owner-{owner_id}"

    async def _has_expired(self) -> bool:
        if self.ttl < 0:  # not cached
            return True
        elapsed_since_stored = self.RETENTION_SECONDS - self.ttl
        return elapsed_since_stored > self.VALIDITY_SECONDS

    @classmethod
    async def delete(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> None:
        await redis.delete(typing.cast(UserTokensSaas, cls)._cache_key(owner_id))

    @classmethod
    async def get(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> UserTokensT:
        return typing.cast(
            UserTokensT,
            await typing.cast(UserTokensSaas, cls)._get(redis, owner_id),
        )

    @classmethod
    async def _get(
        cls, redis: redis_utils.RedisCache, owner_id: int
    ) -> "UserTokensSaas":
        cached_tokens = await cls._retrieve_from_cache(redis, owner_id)
        if cached_tokens is None or await cached_tokens._has_expired():
            try:
                db_tokens = await cls._retrieve_from_db(redis, owner_id)
            except Exception as exc:
                if cached_tokens is not None and (
                    exceptions.should_be_ignored(exc) or exceptions.need_retry(exc)
                ):
                    # NOTE(sileht): return the cached tokens, instead of retring the
                    # stream, just because the dashboard has a connectivity issue.
                    return cached_tokens
                raise
            await db_tokens.save_to_cache()
            return db_tokens
        return cached_tokens

    async def save_to_cache(self) -> None:
        """Save a tokens to the cache."""
        await self.redis.setex(
            self._cache_key(self.owner_id),
            self.RETENTION_SECONDS,
            crypto.encrypt(json.dumps({"user_tokens": self.users}).encode()),
        )
        self.ttl = self.RETENTION_SECONDS

    @classmethod
    async def _retrieve_from_cache(
        cls, redis: redis_utils.RedisCache, owner_id: int
    ) -> typing.Optional["UserTokensSaas"]:
        async with await redis.pipeline() as pipe:
            await pipe.get(cls._cache_key(owner_id))
            await pipe.ttl(cls._cache_key(owner_id))
            encrypted_tokens, ttl = typing.cast(
                typing.Tuple[str, int], await pipe.execute()
            )
        if encrypted_tokens:
            decrypted_tokens = json.loads(
                crypto.decrypt(encrypted_tokens.encode()).decode()
            )

            if "tokens" in decrypted_tokens:
                # Old cache format, just drop it
                return None

            if (
                decrypted_tokens["user_tokens"]
                and "id" not in decrypted_tokens["user_tokens"][0]
            ):
                # Old cache format, just drop it
                return None

            return cls(redis, owner_id, decrypted_tokens["user_tokens"], ttl)
        return None

    @classmethod
    async def _retrieve_from_db(
        cls, redis: redis_utils.RedisCache, owner_id: int
    ) -> "UserTokensSaas":
        async with dashboard.AsyncDashboardSaasClient() as client:
            try:
                resp = await client.get(f"/engine/user_tokens/{owner_id}")
            except http.HTTPNotFound:
                return cls(redis, owner_id, [])
            else:
                tokens = resp.json()
                return cls(redis, owner_id, tokens["user_tokens"])


@dataclasses.dataclass
class UserTokensOnPremise(UserTokensBase):
    @classmethod
    async def delete(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> None:
        pass

    @classmethod
    async def get(
        cls: typing.Type[UserTokensT], redis: redis_utils.RedisCache, owner_id: int
    ) -> UserTokensT:
        return cls(
            redis,
            owner_id,
            [
                {
                    "id": github_types.GitHubAccountIdType(_id),
                    "login": github_types.GitHubLogin(login),
                    "oauth_access_token": github_types.GitHubOAuthToken(
                        oauth_access_token
                    ),
                    "email": None,
                    "name": None,
                }
                for _id, login, oauth_access_token in config.ACCOUNT_TOKENS
            ],
        )


if config.SAAS_MODE:

    @dataclasses.dataclass
    class UserTokens(UserTokensSaas):
        pass

else:

    @dataclasses.dataclass
    class UserTokens(UserTokensOnPremise):  # type: ignore [no-redef]
        pass
