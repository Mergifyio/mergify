# -*- encoding: utf-8 -*-
# mypy: disallow-untyped-defs
#
# Copyright © 2020—2021 Mergify SAS
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
import base64
import contextlib
import dataclasses
import datetime
import functools
import json
import logging
import random
import re
import typing
from urllib import parse

import daiquiri
from ddtrace import tracer
import first
from graphql_utils import multi
import jinja2.exceptions
import jinja2.meta
import jinja2.runtime
import jinja2.sandbox
import jinja2.utils
import markdownify
import msgpack
import tenacity

from mergify_engine import cache
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import date
from mergify_engine import exceptions
from mergify_engine import github_graphql_types
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription as subscription_mod
from mergify_engine.dashboard import user_tokens


if typing.TYPE_CHECKING:
    from mergify_engine import rules

SUMMARY_SHA_EXPIRATION = 60 * 60 * 24 * 31 * 1  # 1 Month

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)

MARKDOWN_COMMENT_RE = re.compile("(<!--.*?-->)", flags=re.DOTALL | re.IGNORECASE)


class MergifyConfigFile(github_types.GitHubContentFile):
    decoded_content: str


def content_file_to_config_file(
    content: github_types.GitHubContentFile,
) -> MergifyConfigFile:
    return MergifyConfigFile(
        type=content["type"],
        content=content["content"],
        path=content["path"],
        sha=content["sha"],
        decoded_content=base64.b64decode(
            bytearray(content["content"], "utf-8")
        ).decode(),
    )


DEFAULT_CONFIG_FILE = MergifyConfigFile(
    decoded_content="",
    type="file",
    content="<default>",
    sha=github_types.SHAType("<default>"),
    path="<default>",
)


class T_PayloadEventSource(typing.TypedDict):
    event_type: github_types.GitHubEventType
    data: github_types.GitHubEvent
    timestamp: str


@dataclasses.dataclass
class PullRequestAttributeError(AttributeError):
    name: str


@dataclasses.dataclass
class InstallationCaches:
    team_members: cache.Cache[
        github_types.GitHubTeamSlug, typing.List[github_types.GitHubLogin]
    ] = dataclasses.field(default_factory=cache.Cache)


@dataclasses.dataclass
class Installation:
    installation: github_types.GitHubInstallation
    subscription: subscription_mod.Subscription = dataclasses.field(repr=False)
    client: github.AsyncGithubInstallationClient = dataclasses.field(repr=False)
    redis: redis_utils.RedisLinks = dataclasses.field(repr=False)

    repositories: "typing.Dict[github_types.GitHubRepositoryName, Repository]" = (
        dataclasses.field(default_factory=dict, repr=False)
    )
    _user_tokens: typing.Optional[user_tokens.UserTokens] = dataclasses.field(
        default=None, repr=False
    )
    _caches: InstallationCaches = dataclasses.field(
        default_factory=InstallationCaches, repr=False
    )

    @property
    def owner_id(self) -> github_types.GitHubAccountIdType:
        return self.installation["account"]["id"]

    @property
    def owner_login(self) -> github_types.GitHubLogin:
        return self.installation["account"]["login"]

    async def get_user_tokens(self) -> user_tokens.UserTokens:
        # NOTE(sileht): For the simulator all contexts are built with a user
        # oauth token, even if it have access to the organization. We don't
        # want to share this. This can't occurs in current code, but this is an
        # easy and strong seatbelt.
        if not isinstance(self.client.auth, github.GithubAppInstallationAuth):
            raise RuntimeError(
                "Installation.get_user_tokens() used with in a non GithubApp context"
            )

        if self._user_tokens is None:
            self._user_tokens = await user_tokens.UserTokens.get(
                self.redis.cache, self.owner_id
            )
        return self._user_tokens

    USER_ID_MAPPING_CACHE_KEY = "user-id-mapping"

    async def get_user(
        self, login: github_types.GitHubLogin
    ) -> github_types.GitHubAccount:
        data = await self.redis.cache.hget(self.USER_ID_MAPPING_CACHE_KEY, login)
        if data is not None:
            return typing.cast(github_types.GitHubAccount, json.loads(data))

        user = typing.cast(
            github_types.GitHubAccount, await self.client.item(f"/users/{login}")
        )
        await self.redis.cache.hset(
            self.USER_ID_MAPPING_CACHE_KEY, login, json.dumps(user)
        )
        return user

    async def get_pull_request_context(
        self,
        repo_id: github_types.GitHubRepositoryIdType,
        pull_number: github_types.GitHubPullRequestNumber,
        force_new: bool = False,
        wait_background_github_processing: bool = False,
    ) -> "Context":
        for repository in self.repositories.values():
            if repository.repo["id"] == repo_id:
                return await repository.get_pull_request_context(
                    pull_number,
                    force_new=force_new,
                    wait_background_github_processing=wait_background_github_processing,
                )

        pull = await self.client.item(f"/repositories/{repo_id}/pulls/{pull_number}")
        repository = self.get_repository_from_github_data(pull["base"]["repo"])
        return await repository.get_pull_request_context(
            pull_number,
            pull,
            force_new=force_new,
            wait_background_github_processing=wait_background_github_processing,
        )

    def get_repository_from_github_data(
        self,
        repo: github_types.GitHubRepository,
    ) -> "Repository":
        if repo["name"] not in self.repositories:
            repository = Repository(self, repo)
            self.repositories[repo["name"]] = repository
        return self.repositories[repo["name"]]

    async def get_repository_by_name(
        self,
        name: github_types.GitHubRepositoryName,
    ) -> "Repository":
        if name in self.repositories:
            return self.repositories[name]
        repo_data: github_types.GitHubRepository = await self.client.item(
            f"/repos/{self.owner_login}/{name}"
        )
        return self.get_repository_from_github_data(repo_data)

    async def get_repository_by_id(
        self, _id: github_types.GitHubRepositoryIdType
    ) -> "Repository":
        for repository in self.repositories.values():
            if repository.repo["id"] == _id:
                return repository
        repo_data: github_types.GitHubRepository = await self.client.item(
            f"/repositories/{_id}"
        )
        return self.get_repository_from_github_data(repo_data)

    TEAM_MEMBERS_CACHE_KEY_PREFIX = "team_members"
    TEAM_MEMBERS_CACHE_KEY_DELIMITER = "/"
    TEAM_MEMBERS_EXPIRATION = 3600  # 1 hour

    @classmethod
    def _team_members_cache_key_for_repo(
        cls,
        owner_id: github_types.GitHubAccountIdType,
    ) -> str:
        return (
            f"{cls.TEAM_MEMBERS_CACHE_KEY_PREFIX}"
            f"{cls.TEAM_MEMBERS_CACHE_KEY_DELIMITER}{owner_id}"
        )

    @classmethod
    async def clear_team_members_cache_for_team(
        cls,
        redis: redis_utils.RedisTeamMembersCache,
        owner: github_types.GitHubAccount,
        team_slug: github_types.GitHubTeamSlug,
    ) -> None:
        await redis.hdel(
            cls._team_members_cache_key_for_repo(owner["id"]),
            team_slug,
        )

    @classmethod
    async def clear_team_members_cache_for_org(
        cls, redis: redis_utils.RedisTeamMembersCache, user: github_types.GitHubAccount
    ) -> None:
        await redis.delete(cls._team_members_cache_key_for_repo(user["id"]))

    async def get_team_members(
        self, team_slug: github_types.GitHubTeamSlug
    ) -> typing.List[github_types.GitHubLogin]:
        members = self._caches.team_members.get(team_slug)
        if members is cache.Unset:
            key = self._team_members_cache_key_for_repo(self.owner_id)
            members_raw = await self.redis.team_members_cache.hget(key, team_slug)
            if members_raw is None:
                members = [
                    github_types.GitHubLogin(member["login"])
                    async for member in self.client.items(
                        f"/orgs/{self.owner_login}/teams/{team_slug}/members",
                        resource_name="team members",
                        page_limit=20,
                    )
                ]
                pipe = await self.redis.team_members_cache.pipeline()
                await pipe.hset(key, team_slug, msgpack.packb(members))
                await pipe.expire(key, self.TEAM_MEMBERS_EXPIRATION)
                await pipe.execute()
            else:
                members = typing.cast(
                    typing.List[github_types.GitHubLogin], msgpack.unpackb(members_raw)
                )
            self._caches.team_members.set(team_slug, members)
        return members


@dataclasses.dataclass
class RepositoryCaches:
    mergify_config_file: cache.SingleCache[
        typing.Optional[MergifyConfigFile]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    mergify_config: cache.SingleCache[
        typing.Union["rules.MergifyConfig", Exception]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    branches: cache.Cache[
        github_types.GitHubRefType, github_types.GitHubBranch
    ] = dataclasses.field(default_factory=cache.Cache)
    labels: cache.SingleCache[
        typing.List[github_types.GitHubLabel]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    branch_protections: cache.Cache[
        github_types.GitHubRefType, typing.Optional[github_types.GitHubBranchProtection]
    ] = dataclasses.field(default_factory=cache.Cache)
    commits: cache.Cache[
        github_types.GitHubRefType, typing.List[github_types.GitHubBranchCommit]
    ] = dataclasses.field(default_factory=cache.Cache)
    user_permissions: cache.Cache[
        github_types.GitHubAccountIdType, github_types.GitHubRepositoryPermission
    ] = dataclasses.field(default_factory=cache.Cache)
    team_has_read_permission: cache.Cache[
        github_types.GitHubTeamSlug, bool
    ] = dataclasses.field(default_factory=cache.Cache)


@dataclasses.dataclass
class Repository(object):
    installation: Installation
    repo: github_types.GitHubRepository
    pull_contexts: "typing.Dict[github_types.GitHubPullRequestNumber, Context]" = (
        dataclasses.field(default_factory=dict, repr=False)
    )

    _caches: RepositoryCaches = dataclasses.field(
        default_factory=RepositoryCaches, repr=False
    )
    log: "logging.LoggerAdapter[logging.Logger]" = dataclasses.field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.log = daiquiri.getLogger(
            self.__class__.__qualname__,
            gh_owner=self.installation.owner_login,
            gh_repo=self.repo["name"],
            gh_private=self.repo["private"],
        )

    @property
    def base_url(self) -> str:
        """The URL prefix to make GitHub request."""
        return f"/repos/{self.installation.owner_login}/{self.repo['name']}"

    async def iter_mergify_config_files(
        self,
        ref: typing.Optional[github_types.SHAType] = None,
        preferred_filename: typing.Optional[str] = None,
    ) -> typing.AsyncIterator[MergifyConfigFile]:
        """Get the Mergify configuration file content.

        :return: The filename and its content.
        """

        params = {}
        if ref:
            params["ref"] = str(ref)

        filenames = constants.MERGIFY_CONFIG_FILENAMES.copy()
        if preferred_filename:
            filenames.remove(preferred_filename)
            filenames.insert(0, preferred_filename)

        for filename in filenames:
            try:
                content = typing.cast(
                    github_types.GitHubContentFile,
                    await self.installation.client.item(
                        f"{self.base_url}/contents/{filename}",
                        params=params,
                    ),
                )
            except http.HTTPNotFound:
                continue
            except http.HTTPForbidden as e:
                codes = [e["code"] for e in e.response.json().get("errors", [])]
                if "too_large" in codes:
                    self.log.warning(
                        "configuration file too big, skipping it.",
                        config_filename=filename,
                    )
                    continue
                raise

            yield content_file_to_config_file(content)

    @tracer.wrap("get_mergify_config", span_type="worker")
    async def get_mergify_config(self) -> "rules.MergifyConfig":
        # circular import
        from mergify_engine import rules

        mergify_config_or_exception = self._caches.mergify_config.get()
        if mergify_config_or_exception is not cache.Unset:
            if isinstance(mergify_config_or_exception, Exception):
                raise mergify_config_or_exception
            else:
                return mergify_config_or_exception

        config_file = await self.get_mergify_config_file()
        if config_file is None:
            config_file = DEFAULT_CONFIG_FILE

        # BRANCH CONFIGURATION CHECKING
        try:
            mergify_config = rules.get_mergify_config(config_file)
        except Exception as e:
            self._caches.mergify_config.set(e)
            raise

        # Add global and mandatory rules
        mergify_config["pull_request_rules"].rules.extend(
            rules.MERGIFY_BUILTIN_CONFIG["pull_request_rules"].rules
        )
        self._caches.mergify_config.set(mergify_config)
        return mergify_config

    async def get_mergify_config_file(self) -> typing.Optional[MergifyConfigFile]:
        mergify_config_file = self._caches.mergify_config_file.get()
        if mergify_config_file is not cache.Unset:
            return mergify_config_file

        cached_config_file = await self.get_cached_config_file(
            self.repo["id"],
        )

        if cached_config_file is not None:
            self._caches.mergify_config_file.set(cached_config_file)
            return cached_config_file

        config_file_cache_key = self.get_config_file_cache_key(self.repo["id"])
        pipeline = await self.installation.redis.cache.pipeline()

        async for config_file in self.iter_mergify_config_files():
            await pipeline.set(
                config_file_cache_key,
                json.dumps(
                    github_types.GitHubContentFile(
                        {
                            "type": config_file["type"],
                            "content": config_file["content"],
                            "path": config_file["path"],
                            "sha": config_file["sha"],
                        }
                    )
                ),
                ex=60 * 60 * 24 * 31,
            )
            self._caches.mergify_config_file.set(config_file)
            await pipeline.execute()
            return config_file

        self._caches.mergify_config_file.set(None)
        return None

    async def get_cached_config_file(
        self,
        repo_id: github_types.GitHubRepositoryIdType,
    ) -> typing.Optional[MergifyConfigFile]:
        config_file_raw = await self.installation.redis.cache.get(
            self.get_config_file_cache_key(repo_id),
        )

        if config_file_raw is None:
            return None

        content = typing.cast(
            github_types.GitHubContentFile, json.loads(config_file_raw)
        )
        return content_file_to_config_file(content)

    async def get_commits(
        self,
        branch_name: github_types.GitHubRefType,
    ) -> typing.List[github_types.GitHubBranchCommit]:
        """Returns the last commits from a branch.

        This only returns the last 100 commits."""

        commits = self._caches.commits.get(branch_name)
        if commits is cache.Unset:
            commits = typing.cast(
                typing.List[github_types.GitHubBranchCommit],
                await self.installation.client.item(
                    f"{self.base_url}/commits",
                    params={"per_page": "100", "sha": branch_name},
                ),
            )
            self._caches.commits.set(branch_name, commits)

        return commits

    async def get_branch(
        self,
        branch_name: github_types.GitHubRefType,
        bypass_cache: bool = False,
    ) -> github_types.GitHubBranch:
        branch = self._caches.branches.get(branch_name)
        if branch is cache.Unset or bypass_cache:
            escaped_branch_name = parse.quote(branch_name, safe="")
            branch = typing.cast(
                github_types.GitHubBranch,
                await self.installation.client.item(
                    f"{self.base_url}/branches/{escaped_branch_name}"
                ),
            )
            self._caches.branches.set(branch_name, branch)
        return branch

    async def get_pull_request_context(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
        pull: typing.Optional[github_types.GitHubPullRequest] = None,
        force_new: bool = False,
        wait_background_github_processing: bool = False,
    ) -> "Context":
        if force_new or pull_number not in self.pull_contexts:
            if pull is None:
                pull = await self.installation.client.item(
                    f"{self.base_url}/pulls/{pull_number}"
                )
            elif pull["number"] != pull_number:
                raise RuntimeError(
                    'get_pull_request_context() needs pull["number"] == pull_number'
                )
            ctxt = await Context.create(
                self,
                pull,
                wait_background_github_processing=wait_background_github_processing,
            )
            self.pull_contexts[pull_number] = ctxt
        elif (
            wait_background_github_processing
            and not self.pull_contexts[
                pull_number
            ].is_background_github_processing_completed()
        ):
            await self.pull_contexts[pull_number].ensure_complete(
                wait_background_github_processing
            )

        return self.pull_contexts[pull_number]

    CONFIG_FILE_CACHE_KEY_PREFIX = "config_file"
    CONFIG_FILE_CACHE_KEY_DELIMITER = "/"

    @classmethod
    def get_config_file_cache_key(
        cls,
        repo_id: github_types.GitHubRepositoryIdType,
    ) -> str:
        return (
            f"{cls.CONFIG_FILE_CACHE_KEY_PREFIX}"
            f"{cls.CONFIG_FILE_CACHE_KEY_DELIMITER}{repo_id}"
        )

    @classmethod
    async def clear_config_file_cache(
        cls,
        redis: redis_utils.RedisCache,
        repo_id: github_types.GitHubRepositoryIdType,
    ) -> None:
        await redis.delete(
            cls.get_config_file_cache_key(repo_id),
        )

    USERS_PERMISSION_CACHE_KEY_PREFIX = "users_permission"
    USERS_PERMISSION_CACHE_KEY_DELIMITER = "/"
    USERS_PERMISSION_EXPIRATION = 3600  # 1 hour

    @classmethod
    def _users_permission_cache_key_for_repo(
        cls,
        owner_id: github_types.GitHubAccountIdType,
        repo_id: github_types.GitHubRepositoryIdType,
    ) -> str:
        return (
            f"{cls.USERS_PERMISSION_CACHE_KEY_PREFIX}"
            f"{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{owner_id}"
            f"{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{repo_id}"
        )

    @property
    def _users_permission_cache_key(self) -> str:
        return self._users_permission_cache_key_for_repo(
            self.installation.owner_id, self.repo["id"]
        )

    @classmethod
    async def clear_user_permission_cache_for_user(
        cls,
        redis: redis_utils.RedisUserPermissionsCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
        user: github_types.GitHubAccount,
    ) -> None:
        await redis.hdel(
            cls._users_permission_cache_key_for_repo(owner["id"], repo["id"]),
            str(user["id"]),
        )

    @classmethod
    async def clear_user_permission_cache_for_repo(
        cls,
        redis: redis_utils.RedisUserPermissionsCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
    ) -> None:
        await redis.delete(
            cls._users_permission_cache_key_for_repo(owner["id"], repo["id"])
        )

    @classmethod
    async def clear_user_permission_cache_for_org(
        cls,
        redis: redis_utils.RedisUserPermissionsCache,
        user: github_types.GitHubAccount,
    ) -> None:
        pipeline = await redis.pipeline()
        async for key in redis.scan_iter(
            f"{cls.USERS_PERMISSION_CACHE_KEY_PREFIX}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{user['id']}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}*",
            count=10000,
        ):
            await pipeline.delete(key)
        await pipeline.execute()

    async def get_user_permission(
        self,
        user: github_types.GitHubAccount,
    ) -> github_types.GitHubRepositoryPermission:
        permission = self._caches.user_permissions.get(user["id"])
        if permission is cache.Unset:
            key = self._users_permission_cache_key
            cached_permission_raw = (
                await self.installation.redis.user_permissions_cache.hget(
                    key, str(user["id"])
                )
            )
            if cached_permission_raw is None:
                permission = typing.cast(
                    github_types.GitHubRepositoryCollaboratorPermission,
                    await self.installation.client.item(
                        f"{self.base_url}/collaborators/{user['login']}/permission"
                    ),
                )["permission"]
                pipe = await self.installation.redis.user_permissions_cache.pipeline()
                await pipe.hset(key, user["id"], permission)
                await pipe.expire(key, self.USERS_PERMISSION_EXPIRATION)
                await pipe.execute()
            else:
                permission = typing.cast(
                    github_types.GitHubRepositoryPermission,
                    cached_permission_raw.decode(),
                )
            self._caches.user_permissions.set(user["id"], permission)
        return permission

    async def has_write_permission(self, user: github_types.GitHubAccount) -> bool:
        permission = await self.get_user_permission(user)
        return permission in ("admin", "maintain", "write")

    TEAMS_PERMISSION_CACHE_KEY_PREFIX = "teams_permission"
    TEAMS_PERMISSION_CACHE_KEY_DELIMITER = "/"
    TEAMS_PERMISSION_EXPIRATION = 3600  # 1 hour

    @classmethod
    def _teams_permission_cache_key_for_repo(
        cls,
        owner_id: github_types.GitHubAccountIdType,
        repo_id: github_types.GitHubRepositoryIdType,
    ) -> str:
        return (
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_PREFIX}"
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}{owner_id}"
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}{repo_id}"
        )

    @property
    def _teams_permission_cache_key(self) -> str:
        return self._teams_permission_cache_key_for_repo(
            self.installation.owner_id, self.repo["id"]
        )

    @classmethod
    async def clear_team_permission_cache_for_team(
        cls,
        redis: redis_utils.RedisTeamPermissionsCache,
        owner: github_types.GitHubAccount,
        team: github_types.GitHubTeamSlug,
    ) -> None:
        pipeline = await redis.pipeline()
        async for key in redis.scan_iter(
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_PREFIX}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}{owner['id']}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}*",
            count=10000,
        ):
            await redis.hdel(key, team)
        await pipeline.execute()

    @classmethod
    async def clear_team_permission_cache_for_repo(
        cls,
        redis: redis_utils.RedisTeamPermissionsCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
    ) -> None:
        await redis.delete(
            cls._teams_permission_cache_key_for_repo(owner["id"], repo["id"])
        )

    @classmethod
    async def clear_team_permission_cache_for_org(
        cls,
        redis: redis_utils.RedisTeamPermissionsCache,
        org: github_types.GitHubAccount,
    ) -> None:
        pipeline = await redis.pipeline()
        async for key in redis.scan_iter(
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_PREFIX}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}{org['id']}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}*",
            count=10000,
        ):
            await pipeline.delete(key)
        await pipeline.execute()

    async def team_has_read_permission(self, team: github_types.GitHubTeamSlug) -> bool:
        read_permission = self._caches.team_has_read_permission.get(team)
        if read_permission is cache.Unset:
            key = self._teams_permission_cache_key
            read_permission_raw = (
                await self.installation.redis.team_permissions_cache.hget(key, team)
            )
            if read_permission_raw is None:
                try:
                    # note(sileht) read permissions are not part of the permissions
                    # list as the api endpoint returns 404 if permission read is missing
                    # so no need to check permission
                    await self.installation.client.get(
                        f"/orgs/{self.installation.owner_login}/teams/{team}/repos/{self.installation.owner_login}/{self.repo['name']}",
                    )
                    read_permission = True
                except http.HTTPNotFound:
                    read_permission = False
                pipe = await self.installation.redis.team_permissions_cache.pipeline()
                await pipe.hset(key, team, str(int(read_permission)))
                await pipe.expire(key, self.TEAMS_PERMISSION_EXPIRATION)
                await pipe.execute()
            else:
                read_permission = bool(int(read_permission_raw))
            self._caches.team_has_read_permission.set(team, read_permission)
        return read_permission

    async def _get_branch_protection_from_branch(
        self,
        branch_name: github_types.GitHubRefType,
    ) -> typing.Optional[github_types.GitHubBranchProtection]:
        try:
            branch = await self.get_branch(branch_name)
        except http.HTTPNotFound:
            return None

        if branch["protection"]["enabled"]:
            return github_types.GitHubBranchProtection(
                {
                    "required_status_checks": branch["protection"][
                        "required_status_checks"
                    ],
                }
            )
        return None

    async def get_branch_protection(
        self,
        branch_name: github_types.GitHubRefType,
    ) -> typing.Optional[github_types.GitHubBranchProtection]:
        branch_protection = self._caches.branch_protections.get(branch_name)
        if branch_protection is cache.Unset:
            escaped_branch_name = parse.quote(branch_name, safe="")
            try:
                branch_protection = typing.cast(
                    github_types.GitHubBranchProtection,
                    await self.installation.client.item(
                        f"{self.base_url}/branches/{escaped_branch_name}/protection",
                        api_version="luke-cage",
                    ),
                )
            except http.HTTPNotFound:
                branch_protection = None
            except http.HTTPForbidden as e:
                if (
                    "or make this repository public to enable this feature."
                    in e.message
                ):
                    branch_protection = None
                elif "Resource not accessible by integration" in e.message:
                    branch_protection = await self._get_branch_protection_from_branch(
                        branch_name
                    )
                else:
                    raise

            self._caches.branch_protections.set(branch_name, branch_protection)
        return branch_protection

    async def get_labels(self) -> typing.List[github_types.GitHubLabel]:
        labels = self._caches.labels.get()
        if labels is cache.Unset:
            labels = [
                label
                async for label in typing.cast(
                    typing.AsyncIterator[github_types.GitHubLabel],
                    self.installation.client.items(
                        f"{self.base_url}/labels",
                        resource_name="labels",
                        page_limit=7,
                    ),
                )
            ]
            self._caches.labels.set(labels)
        return labels

    async def ensure_label_exists(self, label_name: str) -> None:
        labels = await self.get_labels()
        names = [label["name"].lower() for label in labels]
        if label_name.lower() not in names:
            color = f"{random.randrange(16 ** 6):06x}"  # nosec
            try:
                resp = await self.installation.client.post(
                    f"{self.base_url}/labels",
                    json={"name": label_name, "color": color},
                )
            except http.HTTPClientSideError as e:
                self.log.warning(
                    "fail to create label",
                    label=label_name,
                    status_code=e.status_code,
                    error_message=e.message,
                )
                return
            else:
                label = typing.cast(github_types.GitHubLabel, resp.json())
                cached_labels = self._caches.labels.get()
                if cached_labels is not cache.Unset:
                    cached_labels.append(label)

    async def get_commits_diff_count(
        self,
        base_ref: typing.Union[
            github_types.GitHubBaseBranchLabel, github_types.SHAType
        ],
        head_ref: typing.Union[
            github_types.GitHubHeadBranchLabel, github_types.SHAType
        ],
    ) -> typing.Optional[int]:
        try:
            data = typing.cast(
                github_types.GitHubCompareCommits,
                await self.installation.client.item(
                    f"{self.base_url}/compare/{parse.quote(base_ref, safe='')}...{parse.quote(head_ref, safe='')}"
                ),
            )
        except http.HTTPNotFound:
            return None
        else:
            return data["behind_by"]


@dataclasses.dataclass
class ContextCaches:
    review_threads: cache.SingleCache[
        typing.List[github_graphql_types.CachedReviewThread],
    ] = dataclasses.field(default_factory=cache.SingleCache)
    consolidated_reviews: cache.SingleCache[
        typing.Tuple[
            typing.List[github_types.GitHubReview],
            typing.List[github_types.GitHubReview],
        ],
    ] = dataclasses.field(default_factory=cache.SingleCache)
    pull_check_runs: cache.SingleCache[
        typing.List[github_types.CachedGitHubCheckRun]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    pull_statuses: cache.SingleCache[
        typing.List[github_types.GitHubStatus]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    reviews: cache.SingleCache[
        typing.List[github_types.GitHubReview]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    is_behind: cache.SingleCache[bool] = dataclasses.field(
        default_factory=cache.SingleCache
    )
    files: cache.SingleCache[
        typing.List[github_types.CachedGitHubFile]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    commits: cache.SingleCache[
        typing.List[github_types.CachedGitHubBranchCommit]
    ] = dataclasses.field(default_factory=cache.SingleCache)
    commits_behind_count: cache.SingleCache[int] = dataclasses.field(
        default_factory=cache.SingleCache
    )


ContextAttributeType = typing.Union[
    None,
    bool,
    typing.List[str],
    str,
    int,
    datetime.time,
    date.PartialDatetime,
    datetime.datetime,
    datetime.timedelta,
    date.RelativeDatetime,
    typing.List[github_types.SHAType],
    typing.List[github_types.GitHubLogin],
    typing.List[github_types.GitHubBranchCommit],
]


@dataclasses.dataclass
class Context(object):
    repository: Repository
    pull: github_types.GitHubPullRequest
    sources: typing.List[T_PayloadEventSource] = dataclasses.field(default_factory=list)
    configuration_changed: bool = False
    pull_request: "PullRequest" = dataclasses.field(init=False, repr=False)
    log: "logging.LoggerAdapter[logging.Logger]" = dataclasses.field(
        init=False, repr=False
    )

    _caches: ContextCaches = dataclasses.field(
        default_factory=ContextCaches, repr=False
    )

    @property
    def redis(self) -> redis_utils.RedisLinks:
        # TODO(sileht): remove me when context split if done
        return self.repository.installation.redis

    @property
    def subscription(self) -> subscription_mod.Subscription:
        # TODO(sileht): remove me when context split if done
        return self.repository.installation.subscription

    @property
    def client(self) -> github.AsyncGithubInstallationClient:
        # TODO(sileht): remove me when context split if done
        return self.repository.installation.client

    @property
    def base_url(self) -> str:
        # TODO(sileht): remove me when context split if done
        return self.repository.base_url

    async def retrieve_unverified_commits(self) -> typing.List[str]:
        return [
            commit["commit_message"]
            for commit in await self.commits
            if not commit["commit_verification_verified"]
        ]

    async def retrieve_review_threads(
        self,
    ) -> typing.List[github_graphql_types.CachedReviewThread]:
        review_threads = self._caches.review_threads.get()
        if review_threads is cache.Unset:
            query = """
                repository(owner: "{owner}", name: "{name}") {{
                    pullRequest(number: {number}) {{
                        reviewThreads(first: 100{after}) {{
                        edges {{
                            node {{
                            isResolved
                            comments(first: 1) {{
                                edges {{
                                    node {{
                                        body
                                    }}
                                }}
                            }}
                            }}
                        }}
                        }}
                    }}
                }}
            """
            responses = typing.cast(
                typing.AsyncIterable[
                    typing.Dict[str, github_graphql_types.GraphqlRepository]
                ],
                multi.multi_query(
                    query,
                    iterable=(
                        {
                            "owner": self.repository.repo["owner"]["login"],
                            "name": self.repository.repo["name"],
                            "number": self.pull["number"],
                        },
                    ),
                    send_fn=self.client.graphql_post,
                ),
            )
            review_threads = []
            async for response in responses:
                for _current_query, current_response in response.items():
                    for thread in current_response["pullRequest"]["reviewThreads"][
                        "edges"
                    ]:
                        review_threads.append(
                            github_graphql_types.CachedReviewThread(
                                {
                                    "isResolved": thread["node"]["isResolved"],
                                    "first_comment": thread["node"]["comments"][
                                        "edges"
                                    ][0]["node"]["body"],
                                }
                            )
                        )

            self._caches.review_threads.set(review_threads)
        return review_threads

    @classmethod
    async def create(
        cls,
        repository: Repository,
        pull: github_types.GitHubPullRequest,
        sources: typing.Optional[typing.List[T_PayloadEventSource]] = None,
        wait_background_github_processing: bool = False,
    ) -> "Context":
        if sources is None:
            sources = []
        self = cls(repository, pull, sources)
        await self.ensure_complete(wait_background_github_processing)
        self.pull_request = PullRequest(self)

        self.log = daiquiri.getLogger(
            self.__class__.__qualname__,
            gh_owner=self.pull["base"]["user"]["login"]
            if "base" in self.pull
            else "<unknown-yet>",
            gh_repo=(
                self.pull["base"]["repo"]["name"]
                if "base" in self.pull
                else "<unknown-yet>"
            ),
            gh_private=(
                self.pull["base"]["repo"]["private"]
                if "base" in self.pull
                else "<unknown-yet>"
            ),
            gh_branch=self.pull["base"]["ref"]
            if "base" in self.pull
            else "<unknown-yet>",
            gh_pull=self.pull["number"],
            gh_pull_base_sha=self.pull["base"]["sha"]
            if "base" in self.pull
            else "<unknown-yet>",
            gh_pull_head_sha=self.pull["head"]["sha"]
            if "head" in self.pull
            else "<unknown-yet>",
            gh_pull_merge_commit_sha=self.pull["merge_commit_sha"],
            gh_pull_url=self.pull.get("html_url", "<unknown-yet>"),
            gh_pull_state=(
                "merged"
                if self.pull.get("merged")
                else (self.pull.get("mergeable_state", "unknown") or "none")
            ),
        )
        return self

    async def set_summary_check(
        self,
        result: check_api.Result,
    ) -> github_types.CachedGitHubCheckRun:
        """Set the Mergify Summary check result."""

        previous_sha = await self.get_cached_last_summary_head_sha()
        # NOTE(sileht): we first commit in redis the future sha,
        # so engine.create_initial_summary() cannot creates a second SUMMARY
        # We don't delete the old redis_last_summary_pulls_key in case of the
        # API call fails, so no other pull request can takeover this sha
        await self._save_cached_last_summary_head_sha(self.pull["head"]["sha"])

        try:
            return await check_api.set_check_run(
                self,
                constants.SUMMARY_NAME,
                result,
                external_id=str(self.pull["number"]),
                skip_cache=self._caches.pull_check_runs.get() is cache.Unset,
            )
        except Exception:
            if previous_sha:
                # Restore previous sha in redis
                await self._save_cached_last_summary_head_sha(
                    previous_sha, self.pull["head"]["sha"]
                )
            raise

    @staticmethod
    def redis_last_summary_head_sha_key(pull: github_types.GitHubPullRequest) -> str:
        owner = pull["base"]["repo"]["owner"]["id"]
        repo = pull["base"]["repo"]["id"]
        pull_number = pull["number"]
        return f"summary-sha~{owner}~{repo}~{pull_number}"

    @staticmethod
    def redis_last_summary_pulls_key(
        owner_id: github_types.GitHubAccountIdType,
        repo_id: github_types.GitHubRepositoryIdType,
        sha: github_types.SHAType,
    ) -> str:
        return f"summary-pulls~{owner_id}~{repo_id}~{sha}"

    @classmethod
    async def get_cached_last_summary_head_sha_from_pull(
        cls,
        redis_cache: redis_utils.RedisCache,
        pull: github_types.GitHubPullRequest,
    ) -> typing.Optional[github_types.SHAType]:
        return typing.cast(
            typing.Optional[github_types.SHAType],
            await redis_cache.get(cls.redis_last_summary_head_sha_key(pull)),
        )

    @classmethod
    async def summary_exists(
        cls,
        redis_cache: redis_utils.RedisCache,
        owner_id: github_types.GitHubAccountIdType,
        repo_id: github_types.GitHubRepositoryIdType,
        pull: github_types.GitHubPullRequest,
    ) -> bool:
        sha_exists = bool(
            await redis_cache.exists(
                cls.redis_last_summary_pulls_key(owner_id, repo_id, pull["head"]["sha"])
            )
        )
        if sha_exists:
            return True

        sha = await cls.get_cached_last_summary_head_sha_from_pull(redis_cache, pull)
        return sha is not None and sha == pull["head"]["sha"]

    async def get_cached_last_summary_head_sha(
        self,
    ) -> typing.Optional[github_types.SHAType]:
        return await self.get_cached_last_summary_head_sha_from_pull(
            self.redis.cache,
            self.pull,
        )

    async def clear_cached_last_summary_head_sha(self) -> None:
        pipe = await self.redis.cache.pipeline()
        await pipe.delete(self.redis_last_summary_head_sha_key(self.pull))
        await pipe.delete(
            self.redis_last_summary_pulls_key(
                self.repository.installation.owner_id,
                self.repository.repo["id"],
                self.pull["head"]["sha"],
            ),
            self.pull["number"],
        )
        await pipe.execute()

    async def _save_cached_last_summary_head_sha(
        self,
        sha: github_types.SHAType,
        old_sha: typing.Optional[github_types.SHAType] = None,
    ) -> None:
        # NOTE(sileht): We store it only for 1 month, if we lose it it's not a big deal, as it's just
        # to avoid race conditions when too many synchronize events occur in a short period of time
        pipe = await self.redis.cache.pipeline()
        await pipe.set(
            self.redis_last_summary_head_sha_key(self.pull),
            sha,
            ex=SUMMARY_SHA_EXPIRATION,
        )
        await pipe.set(
            self.redis_last_summary_pulls_key(
                self.repository.installation.owner_id,
                self.repository.repo["id"],
                sha,
            ),
            self.pull["number"],
            ex=SUMMARY_SHA_EXPIRATION,
        )
        if old_sha is not None:
            await pipe.delete(
                self.redis_last_summary_pulls_key(
                    self.repository.installation.owner_id,
                    self.repository.repo["id"],
                    old_sha,
                ),
            )
        await pipe.execute()

    async def consolidated_reviews(
        self,
    ) -> typing.Tuple[
        typing.List[github_types.GitHubReview], typing.List[github_types.GitHubReview]
    ]:
        consolidated_reviews = self._caches.consolidated_reviews.get()
        if consolidated_reviews is cache.Unset:
            # Ignore reviews that are not from someone with admin/write permissions
            # And only keep the last review for each user.
            comments: typing.Dict[
                github_types.GitHubLogin, github_types.GitHubReview
            ] = {}
            approvals: typing.Dict[
                github_types.GitHubLogin, github_types.GitHubReview
            ] = {}
            valid_user_ids = {
                r["user"]["id"]
                for r in await self.reviews
                if (
                    r["user"] is not None
                    and (
                        r["user"]["type"] == "Bot"
                        or await self.repository.has_write_permission(r["user"])
                    )
                )
            }

            for review in await self.reviews:
                if not review["user"] or review["user"]["id"] not in valid_user_ids:
                    continue
                # Only keep latest review of an user
                if review["state"] == "COMMENTED":
                    comments[review["user"]["login"]] = review
                else:
                    approvals[review["user"]["login"]] = review

            consolidated_reviews = list(comments.values()), list(approvals.values())
            self._caches.consolidated_reviews.set(consolidated_reviews)
        return consolidated_reviews

    async def _get_consolidated_data(self, name: str) -> ContextAttributeType:

        if name == "assignee":
            return [a["login"] for a in self.pull["assignees"]]

        elif name == "queue-position":
            q = await merge_train.Train.from_context(self)
            position, _ = q.find_embarked_pull(self.pull["number"])
            if position is None:
                return -1
            return position

        elif name in ("queued-at", "queued-at-relative"):
            q = await merge_train.Train.from_context(self)
            position, embarked_pull = q.find_embarked_pull(self.pull["number"])
            if embarked_pull is None:
                return None
            elif name == "queued-at":
                return embarked_pull.embarked_pull.queued_at
            else:
                return date.RelativeDatetime(embarked_pull.embarked_pull.queued_at)

        elif name in ("queue-merge-started-at", "queue-merge-started-at-relative"):
            # Only used with QueuePullRequest
            q = await merge_train.Train.from_context(self)
            car = q.get_car_by_tmp_pull(self)
            if car is None:
                car = q.get_car(self)
                if car is None:
                    return None
                else:
                    started_at = car.creation_date
            else:
                started_at = car.creation_date
            if name == "queue-merge-started-at":
                return started_at
            else:
                return date.RelativeDatetime(started_at)

        elif name == "label":
            return [label["name"] for label in self.pull["labels"]]

        elif name == "review-requested":
            return (
                [typing.cast(str, u["login"]) for u in self.pull["requested_reviewers"]]
                + [f"@{t['slug']}" for t in self.pull["requested_teams"]]
                + [
                    f"@{self.repository.installation.owner_login}/{t['slug']}"
                    for t in self.pull["requested_teams"]
                ]
            )
        elif name == "draft":
            return self.pull["draft"]

        elif name == "author":
            return self.pull["user"]["login"]

        elif name == "merged-by":
            return (
                self.pull["merged_by"]["login"]
                if self.pull["merged_by"] is not None
                else ""
            )

        elif name == "merged":
            return self.pull["merged"]

        elif name == "closed":
            return self.closed

        elif name == "milestone":
            return (
                self.pull["milestone"]["title"]
                if self.pull["milestone"] is not None
                else ""
            )

        elif name == "number":
            return typing.cast(int, self.pull["number"])

        elif name == "#commits-behind":
            return await self.commits_behind_count

        elif name == "conflict":
            return self.pull["mergeable"] is False

        elif name == "linear-history":
            return all(len(commit["parents"]) == 1 for commit in await self.commits)

        elif name == "base":
            return self.pull["base"]["ref"]

        elif name == "head":
            return self.pull["head"]["ref"]

        elif name == "locked":
            return self.pull["locked"]

        elif name == "title":
            return self.pull["title"]

        elif name == "body":
            return MARKDOWN_COMMENT_RE.sub(
                "",
                self.body,
            )

        elif name == "body-raw":
            return self.body

        elif name == "#files":
            return self.pull["changed_files"]

        elif name == "files":
            return [f["filename"] for f in await self.files]

        elif name == "#commits":
            return self.pull["commits"]

        elif name == "commits":
            return [c["commit_message"] for c in await self.commits]

        elif name == "approved-reviews-by":
            _, approvals = await self.consolidated_reviews()
            return [r["user"]["login"] for r in approvals if r["state"] == "APPROVED"]
        elif name == "dismissed-reviews-by":
            _, approvals = await self.consolidated_reviews()
            return [r["user"]["login"] for r in approvals if r["state"] == "DISMISSED"]
        elif name == "changes-requested-reviews-by":
            _, approvals = await self.consolidated_reviews()
            return [
                r["user"]["login"]
                for r in approvals
                if r["state"] == "CHANGES_REQUESTED"
            ]
        elif name == "commented-reviews-by":
            comments, _ = await self.consolidated_reviews()
            return [r["user"]["login"] for r in comments if r["state"] == "COMMENTED"]

        elif name == "check-success-or-neutral-or-pending":
            return [
                ctxt
                for ctxt, state in (await self.checks).items()
                if state in ("success", "neutral", "pending", None)
            ]

        # NOTE(jd) The Check API set conclusion to None for pending.
        elif name == "check-success-or-neutral":
            return [
                ctxt
                for ctxt, state in (await self.checks).items()
                if state in ("success", "neutral")
            ]
        elif name in ("status-success", "check-success"):
            return [
                ctxt
                for ctxt, state in (await self.checks).items()
                if state == "success"
            ]
        elif name in ("status-failure", "check-failure"):
            # hopefully "cancelled" is actually a failure state to github.
            # I think it is, however it could be the same thing as the
            # "skipped" status.
            return [
                ctxt
                for ctxt, state in (await self.checks).items()
                if state
                in ["failure", "action_required", "cancelled", "timed_out", "error"]
            ]
        elif name in ("status-neutral", "check-neutral"):
            return [
                ctxt
                for ctxt, state in (await self.checks).items()
                if state == "neutral"
            ]
        elif name == "check-skipped":
            # hopefully this handles the gray "skipped" state that github actions
            # workflows can send when a job that depends on a job and the job it
            # depends on fails, making it get skipped automatically then.
            return [
                ctext
                for ctext, state in (await self.checks).items()
                if state == "skipped"
            ]
        elif name == "check":
            return [ctext for ctext, state in (await self.checks).items()]
        elif name == "check-pending":
            return [
                ctext
                for ctext, state in (await self.checks).items()
                if state in (None, "pending")
            ]
        elif name == "check-stale":
            return [
                ctext
                for ctext, state in (await self.checks).items()
                if state == "stale"
            ]
        elif name == "depends-on":
            # TODO(sileht):  This is the list of merged pull requests that are
            # required by this pull request. An optimisation can be to look at
            # the merge queues too, to queue this pull request earlier
            depends_on = []
            for pull_request_number in self.get_depends_on():
                try:
                    ctxt = await self.repository.get_pull_request_context(
                        pull_request_number
                    )
                except http.HTTPNotFound:
                    continue
                if ctxt.pull["merged"]:
                    depends_on.append(f"#{pull_request_number}")
            return depends_on
        elif name in ("current-timestamp", "current-time"):
            return date.utcnow()
        elif name == "current-day":
            return date.Day(date.utcnow().day)
        elif name == "current-month":
            return date.Month(date.utcnow().month)
        elif name == "current-year":
            return date.Year(date.utcnow().year)
        elif name == "current-day-of-week":
            return date.DayOfWeek(date.utcnow().isoweekday())

        elif name == "updated-at-relative":
            return date.RelativeDatetime(date.fromisoformat(self.pull["updated_at"]))
        elif name == "created-at-relative":
            return date.RelativeDatetime(date.fromisoformat(self.pull["created_at"]))
        elif name == "closed-at-relative":
            if self.pull["closed_at"] is None:
                return None
            return date.RelativeDatetime(date.fromisoformat(self.pull["closed_at"]))
        elif name == "merged-at-relative":
            if self.pull["merged_at"] is None:
                return None
            return date.RelativeDatetime(date.fromisoformat(self.pull["merged_at"]))

        elif name == "updated-at":
            return date.fromisoformat(self.pull["updated_at"])
        elif name == "created-at":
            return date.fromisoformat(self.pull["created_at"])
        elif name == "closed-at":
            if self.pull["closed_at"] is None:
                return None
            return date.fromisoformat(self.pull["closed_at"])
        elif name == "merged-at":
            if self.pull["merged_at"] is None:
                return None
            return date.fromisoformat(self.pull["merged_at"])
        elif name == "commits-unverified":
            return await self.retrieve_unverified_commits()
        elif name == "review-threads-resolved":
            return [
                t["first_comment"]
                for t in await self.retrieve_review_threads()
                if t["isResolved"]
            ]
        elif name == "review-threads-unresolved":
            return [
                t["first_comment"]
                for t in await self.retrieve_review_threads()
                if not t["isResolved"]
            ]
        elif name == "repository-name":
            return self.repository.repo["name"]
        elif name == "repository-full-name":
            return self.repository.repo["full_name"]
        else:
            raise PullRequestAttributeError(name)

    DEPENDS_ON = re.compile(
        r"^ *Depends-On: +(?:#|"
        + config.GITHUB_URL
        + r"/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/)(?P<pull>\d+) *$",
        re.MULTILINE | re.IGNORECASE,
    )

    @property
    def body(self) -> str:
        # NOTE(sileht): multiline regex on our side assume eol char is only LF,
        # not CR. So ensure we don't have CRLF in the body
        if self.pull["body"] is None:
            return ""
        return self.pull["body"].replace("\r\n", "\n")

    def get_depends_on(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return sorted(
            {
                github_types.GitHubPullRequestNumber(int(pull))
                for owner, repo, pull in self.DEPENDS_ON.findall(self.body)
                if (owner == "" and repo == "")
                or (
                    owner == self.pull["base"]["user"]["login"]
                    and repo == self.pull["base"]["repo"]["name"]
                )
            }
        )

    async def update_cached_check_runs(
        self, check: github_types.CachedGitHubCheckRun
    ) -> None:
        if self._caches.pull_check_runs.get() is cache.Unset:
            return

        pull_check_runs = [
            c for c in await self.pull_check_runs if c["name"] != check["name"]
        ]
        pull_check_runs.append(check)
        self._caches.pull_check_runs.set(pull_check_runs)

    @property
    async def pull_check_runs(self) -> typing.List[github_types.CachedGitHubCheckRun]:
        checks = self._caches.pull_check_runs.get()
        if checks is cache.Unset:
            checks = await check_api.get_checks_for_ref(self, self.pull["head"]["sha"])
            self._caches.pull_check_runs.set(checks)
        return checks

    @property
    async def pull_engine_check_runs(
        self,
    ) -> typing.List[github_types.CachedGitHubCheckRun]:
        return [
            c
            for c in await self.pull_check_runs
            if c["app_id"] == config.INTEGRATION_ID
        ]

    async def get_engine_check_run(
        self, name: str
    ) -> typing.Optional[github_types.CachedGitHubCheckRun]:
        return first.first(
            await self.pull_engine_check_runs, key=lambda c: c["name"] == name
        )

    @property
    async def pull_statuses(self) -> typing.List[github_types.GitHubStatus]:
        statuses = self._caches.pull_statuses.get()
        if statuses is cache.Unset:
            statuses = [
                s
                async for s in typing.cast(
                    typing.AsyncIterable[github_types.GitHubStatus],
                    self.client.items(
                        f"{self.base_url}/commits/{self.pull['head']['sha']}/status",
                        list_items="statuses",
                        resource_name="statuses",
                        page_limit=10,
                    ),
                )
            ]
            self._caches.pull_statuses.set(statuses)
        return statuses

    @property
    async def checks(
        self,
    ) -> typing.Dict[
        str,
        typing.Union[
            github_types.GitHubCheckRunConclusion, github_types.GitHubStatusState
        ],
    ]:
        # NOTE(sileht): check-runs are returned in reverse chronogical order,
        # so if it has ran twice we must keep only the more recent
        # statuses are good as GitHub already ensures the uniqueness of the name

        checks: typing.Dict[
            str,
            typing.Union[
                github_types.GitHubCheckRunConclusion, github_types.GitHubStatusState
            ],
        ] = {}

        # First put all branch protections checks as pending and then override with
        # the real status
        protection = await self.repository.get_branch_protection(
            self.pull["base"]["ref"]
        )
        if (
            protection
            and "required_status_checks" in protection
            and protection["required_status_checks"]
        ):
            checks.update(
                {
                    context: "pending"
                    for context in protection["required_status_checks"]["contexts"]
                }
            )

        # NOTE(sileht): conclusion can be one of success, failure, neutral,
        # cancelled, timed_out, or action_required, and  None for "pending"
        checks.update(
            {
                c["name"]: c["conclusion"]
                for c in sorted(await self.pull_check_runs, key=self._check_runs_sorter)
            }
        )
        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        checks.update({s["context"]: s["state"] for s in await self.pull_statuses})
        return checks

    @staticmethod
    def _check_runs_sorter(
        check_run: github_types.CachedGitHubCheckRun,
    ) -> datetime.datetime:
        if check_run["completed_at"] is None:
            return datetime.datetime.max
        else:
            return datetime.datetime.fromisoformat(check_run["completed_at"][:-1])

    UNUSABLE_STATES = ["unknown", None]

    @tracer.wrap("ensure_complete", span_type="worker")
    async def ensure_complete(self, wait_background_github_processing: bool) -> None:
        if not (
            self._is_data_complete()
            and (
                not wait_background_github_processing
                or self.is_background_github_processing_completed()
            )
        ):
            self.pull = await self.client.item(
                f"{self.base_url}/pulls/{self.pull['number']}"
            )

        if (
            not wait_background_github_processing
            or self.is_background_github_processing_completed()
        ):
            return

        raise exceptions.MergeableStateUnknown(self)

    def _is_data_complete(self) -> bool:
        # NOTE(sileht): If pull request come from /pulls listing or check-runs sometimes,
        # they are incomplete, This ensure we have the complete view
        fields_to_control = (
            "state",
            "mergeable",
            "mergeable_state",
            "merge_commit_sha",
            "merged_by",
            "merged",
            "merged_at",
        )
        for field in fields_to_control:
            if field not in self.pull:
                return False
        return True

    def is_background_github_processing_completed(self) -> bool:
        return self.closed or (
            self.pull["mergeable_state"] not in self.UNUSABLE_STATES
            and self.pull["mergeable"] is not None
        )

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_never,
        reraise=True,
    )
    async def update(self, wait_merged: bool = False) -> None:
        # Don't use it, because consolidated data are not updated after that.
        # Only used by merge/queue action for posting an update report after rebase.
        self.pull = await self.client.item(
            f"{self.base_url}/pulls/{self.pull['number']}"
        )
        if wait_merged and not self.pull["merged"]:
            raise tenacity.TryAgain
        self._caches.pull_check_runs.delete()

    async def _get_external_parents(self) -> typing.Set[github_types.SHAType]:
        known_commits_sha = [commit["sha"] for commit in await self.commits]
        external_parents_sha = set()
        for commit in await self.commits:
            for parent_sha in commit["parents"]:
                if parent_sha not in known_commits_sha:
                    external_parents_sha.add(parent_sha)
        return external_parents_sha

    @property
    async def commits_behind_count(self) -> int:
        commits_behind_count = self._caches.commits_behind_count.get()
        if commits_behind_count is cache.Unset:
            if self.pull["merged"]:
                commits_behind_count = 0
            else:
                commits_diff_count = await self.repository.get_commits_diff_count(
                    self.pull["base"]["label"], self.pull["head"]["label"]
                )
                if commits_diff_count is None:
                    commits_behind_count = 1000000
                else:
                    commits_behind_count = commits_diff_count
            self._caches.commits_behind_count.set(commits_behind_count)
        return commits_behind_count

    @property
    async def is_behind(self) -> bool:
        is_behind = self._caches.is_behind.get()
        if is_behind is cache.Unset:
            # FIXME(sileht): check if we can leverage compare API here like
            # commits_behind_count but using sha instead of label
            branch_name_escaped = parse.quote(self.pull["base"]["ref"], safe="")
            branch = typing.cast(
                github_types.GitHubBranch,
                await self.client.item(
                    f"{self.base_url}/branches/{branch_name_escaped}"
                ),
            )
            external_parents_sha = await self._get_external_parents()
            is_behind = branch["commit"]["sha"] not in external_parents_sha
            diff = await self.repository.get_commits_diff_count(
                branch["commit"]["sha"], self.pull["head"]["sha"]
            )
            if diff is not None:
                is_behind_testing = diff == 0
                if is_behind_testing != is_behind:
                    self.log.info(
                        "is_behind_testing different from expected value",
                        is_behind_testing=is_behind_testing,
                        is_behind=is_behind,
                    )
            else:
                self.log.info("is_behind_testing can't be computed")

            self._caches.is_behind.set(is_behind)
        return is_behind

    def is_merge_queue_pr(self) -> bool:
        return self.pull["title"].startswith("merge-queue:") and self.pull["head"][
            "ref"
        ].startswith(constants.MERGE_QUEUE_BRANCH_PREFIX)

    async def has_been_synchronized_by_user(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, source["data"])
                if event["action"] == "synchronize":
                    is_mergify = event["sender"][
                        "id"
                    ] == config.BOT_USER_ID or await self.redis.cache.get(
                        f"branch-update-{self.pull['head']['sha']}"
                    )
                    if not is_mergify:
                        return True
        return False

    def has_been_synchronized(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, source["data"])
                if event["action"] == "synchronize":
                    return True
        return False

    def has_been_only_refreshed(self) -> bool:
        for source in self.sources:
            if source["event_type"] != "refresh":
                return False
        return True

    def has_been_refreshed_by_timer(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "refresh":
                event = typing.cast(github_types.GitHubEventRefresh, source["data"])
                if (
                    event["action"] == "internal"
                    and event["source"] == "delayed-refresh"
                ):
                    return True
        return False

    def has_been_opened(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, source["data"])
                if event["action"] == "opened":
                    return True
        return False

    def __str__(self) -> str:
        login = self.pull["base"]["user"]["login"]
        repo = self.pull["base"]["repo"]["name"]
        number = self.pull["number"]
        branch = self.pull["base"]["ref"]
        return f"{login}/{repo}/pull/{number}@{branch}"

    @property
    async def reviews(self) -> typing.List[github_types.GitHubReview]:
        reviews = self._caches.reviews.get()
        if reviews is cache.Unset:
            reviews = [
                review
                async for review in typing.cast(
                    typing.AsyncIterable[github_types.GitHubReview],
                    self.client.items(
                        f"{self.base_url}/pulls/{self.pull['number']}/reviews",
                        resource_name="reviews",
                        page_limit=5,
                    ),
                )
            ]
            self._caches.reviews.set(reviews)
        return reviews

    @property
    async def commits(self) -> typing.List[github_types.CachedGitHubBranchCommit]:
        commits = self._caches.commits.get()
        if commits is cache.Unset:
            commits = [
                github_types.to_cached_github_branch_commit(commit)
                async for commit in typing.cast(
                    typing.AsyncIterable[github_types.GitHubBranchCommit],
                    self.client.items(
                        f"{self.base_url}/pulls/{self.pull['number']}/commits",
                        resource_name="commits",
                        page_limit=5,
                    ),
                )
            ]
            if len(commits) >= 250:
                self.log.warning("more than 250 commits found, is_behind maybe wrong")
            self._caches.commits.set(commits)
        return commits

    @property
    async def files(self) -> typing.List[github_types.CachedGitHubFile]:
        files = self._caches.files.get()
        if files is cache.Unset:
            try:
                files = [
                    github_types.CachedGitHubFile({"filename": file["filename"]})
                    async for file in typing.cast(
                        typing.AsyncIterable[github_types.GitHubFile],
                        self.client.items(
                            f"{self.base_url}/pulls/{self.pull['number']}/files",
                            resource_name="files",
                            page_limit=10,
                        ),
                    )
                ]
            except http.HTTPClientSideError as e:
                if (
                    e.status_code == 422
                    and "Sorry, this diff is taking too long to generate" in e.message
                ):
                    raise exceptions.UnprocessablePullRequest(
                        "GitHub cannot generate the file list because the diff is taking too long"
                    )
                raise
            self._caches.files.set(files)
        return files

    @property
    def closed(self) -> bool:
        # NOTE(sileht): GitHub automerge doesn't always close pull requests
        # when it merges them.
        return self.pull["state"] == "closed" or self.pull["merged"]

    @property
    def pull_from_fork(self) -> bool:
        if self.pull["head"]["repo"] is None:
            # Deleted fork repository
            return False
        return self.pull["head"]["repo"]["id"] != self.pull["base"]["repo"]["id"]

    def can_change_github_workflow(self) -> bool:
        workflows_perm = self.repository.installation.installation["permissions"].get(
            "workflows"
        )
        return workflows_perm == "write"

    async def github_workflow_changed(self) -> bool:
        for f in await self.files:
            if f["filename"].startswith(".github/workflows"):
                return True
        return False

    def user_refresh_requested(self) -> bool:
        return any(
            (
                source["event_type"] == "refresh"
                and typing.cast(github_types.GitHubEventRefresh, source["data"])[
                    "action"
                ]
                == "user"
            )
            or (
                source["event_type"] == "check_suite"
                and "action"
                in source["data"]  # TODO(sileht): remove in a couple of days
                and typing.cast(github_types.GitHubEventCheckSuite, source["data"])[
                    "action"
                ]
                == "rerequested"
                and typing.cast(github_types.GitHubEventCheckSuite, source["data"])[
                    "app"
                ]["id"]
                == config.INTEGRATION_ID
            )
            or (
                source["event_type"] == "check_run"
                and "action"
                in source["data"]  # TODO(sileht): remove in a couple of days
                and typing.cast(github_types.GitHubEventCheckRun, source["data"])[
                    "action"
                ]
                == "rerequested"
                and typing.cast(github_types.GitHubEventCheckRun, source["data"])[
                    "app"
                ]["id"]
                == config.INTEGRATION_ID
            )
            for source in self.sources
        )

    def admin_refresh_requested(self) -> bool:
        return any(
            (
                source["event_type"] == "refresh"
                and typing.cast(github_types.GitHubEventRefresh, source["data"])[
                    "action"
                ]
                == "admin"
            )
            for source in self.sources
        )


@dataclasses.dataclass
class RenderTemplateFailure(Exception):
    message: str
    lineno: typing.Optional[int] = None

    def __str__(self) -> str:
        return self.message


class BasePullRequest:
    pass


@dataclasses.dataclass
class PullRequest(BasePullRequest):
    """A high level pull request object.

    This object is used for templates and rule evaluations.
    """

    context: Context

    ATTRIBUTES = {
        "author",
        "merged-by",
        "merged",
        "closed",
        "milestone",
        "number",
        "conflict",
        "linear-history",
        "base",
        "head",
        "locked",
        "title",
        "body",
        "body-raw",
        "queue-position",
    }

    LIST_ATTRIBUTES = {
        "assignee",
        "label",
        "review-requested",
        "approved-reviews-by",
        "dismissed-reviews-by",
        "changes-requested-reviews-by",
        "commented-reviews-by",
        "check-success",
        "check-success-or-neutral",
        "check-failure",
        "check-neutral",
        "status-success",
        "status-failure",
        "status-neutral",
        "check-skipped",
        "check-pending",
        "check-stale",
        "commits",
        "commits-unverified",
        "review-threads-resolved",
        "review-threads-unresolved",
        "repository-name",
        "repository-full-name",
        "files",
    }

    LIST_ATTRIBUTES_WITH_LENGTH_OPTIMIZATION = {
        "#files",
        "#commits",
        "#commits-behind",
    }

    async def __getattr__(self, name: str) -> ContextAttributeType:
        return await self.context._get_consolidated_data(name.replace("_", "-"))

    def __iter__(self) -> typing.Iterator[str]:
        return iter(
            self.ATTRIBUTES
            | self.LIST_ATTRIBUTES
            | self.LIST_ATTRIBUTES_WITH_LENGTH_OPTIMIZATION
        )

    async def items(self) -> typing.Dict[str, ContextAttributeType]:
        d = {}
        for k in self:
            d[k] = await getattr(self, k)
        return d

    async def render_template(
        self,
        template: str,
        extra_variables: typing.Optional[
            typing.Dict[str, typing.Union[str, bool]]
        ] = None,
        allow_get_section: bool = True,
    ) -> str:
        """Render a template interpolating variables based on pull request attributes."""
        env = jinja2.sandbox.SandboxedEnvironment(
            undefined=jinja2.StrictUndefined, enable_async=True
        )
        env.filters["markdownify"] = lambda s: markdownify.markdownify(s)
        if allow_get_section:
            env.filters["get_section"] = functools.partial(
                self._filter_get_section, self
            )

        with self._template_exceptions_mapping():
            used_variables = jinja2.meta.find_undeclared_variables(env.parse(template))
            infos = {}
            for k in used_variables:
                if extra_variables and k in extra_variables:
                    infos[k] = extra_variables[k]
                else:
                    infos[k] = await getattr(self, k)
            return await env.from_string(template).render_async(**infos)

    @staticmethod
    async def _filter_get_section(
        pull: "PullRequest", v: str, section: str, default: typing.Optional[str] = None
    ) -> str:
        if not isinstance(section, str):
            raise jinja2.exceptions.TemplateError("level must be a string")

        section_escaped = re.escape(section)
        level = MARKDOWN_TITLE_RE.match(section)

        if level is None:
            raise jinja2.exceptions.TemplateError("section level not found")

        level_str = level[0].strip()

        level_re = re.compile(rf"^{level_str} +", re.I)
        section_re = re.compile(rf"^{section_escaped}\s*$", re.I)

        found = False
        section_lines = []
        for line in v.split("\n"):
            if section_re.match(line):
                found = True
            elif found and level_re.match(line):
                break
            elif found:
                section_lines.append(line.strip())

        if found:
            text = ("\n".join(section_lines)).strip()
        elif default is None:
            raise jinja2.exceptions.TemplateError("section not found")
        else:
            text = default

        # We don't allow get_section to avoid never-ending recursion
        return await pull.render_template(text, allow_get_section=False)

    @staticmethod
    @contextlib.contextmanager
    def _template_exceptions_mapping() -> typing.Iterator[None]:
        try:
            yield
        except jinja2.exceptions.TemplateSyntaxError as tse:
            raise RenderTemplateFailure(tse.message or "", tse.lineno)
        except jinja2.exceptions.TemplateError as te:
            raise RenderTemplateFailure(te.message or "")
        except PullRequestAttributeError as e:
            raise RenderTemplateFailure(f"Unknown pull request attribute: {e.name}")

    async def get_commit_message(
        self,
        template: typing.Optional[str] = None,
    ) -> typing.Optional[typing.Tuple[str, str]]:

        if template is None:
            # No template from configuration, looks at template from body
            body = typing.cast(str, await self.body)
            if not body:
                return None
            found = False
            message_lines = []

            for line in body.split("\n"):
                if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                    found = True
                elif found and MARKDOWN_TITLE_RE.match(line):
                    break
                elif found:
                    message_lines.append(line)
                if found:
                    self.context.log.info("Commit message template found in body")
                    template = "\n".join(line.strip() for line in message_lines)

        if template is None:
            return None

        commit_message = await self.render_template(template.strip())
        if not commit_message:
            return None

        template_title, _, template_message = commit_message.partition("\n")
        return (template_title, template_message.lstrip())


@dataclasses.dataclass
class QueuePullRequest(BasePullRequest):
    """Same as PullRequest but for temporary pull request used by merge train.

    This object is used for templates and rule evaluations.
    """

    context: Context
    queue_context: Context

    # These attributes are evaluated on the temporary pull request or are
    # always the same within the same batch
    QUEUE_ATTRIBUTES = (
        "base",
        "status-success",
        "status-failure",
        "status-neutral",
        "check",
        "check-success",
        "check-success-or-neutral",
        "check-success-or-neutral-or-pending",
        "check-failure",
        "check-neutral",
        "check-skipped",
        "check-pending",
        "check-stale",
        "current-time",
        "current-day",
        "current-month",
        "current-year",
        "current-day-of-week",
        "current-timestamp",
        "schedule",
        "queue-merge-started-at",
        "queue-merge-started-at-relative",
        "files",
    )

    async def __getattr__(self, name: str) -> ContextAttributeType:
        fancy_name = name.replace("_", "-")
        if fancy_name in self.QUEUE_ATTRIBUTES:
            return await self.queue_context._get_consolidated_data(fancy_name)
        else:
            return await self.context._get_consolidated_data(fancy_name)


# circular import
from mergify_engine.queue import merge_train  # noqa
