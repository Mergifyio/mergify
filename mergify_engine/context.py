# -*- encoding: utf-8 -*-
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
import json
import logging
import re
import typing
from urllib import parse

import daiquiri
import first
import jinja2.exceptions
import jinja2.meta
import jinja2.runtime
import jinja2.sandbox
import jinja2.utils
import tenacity

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import date
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import user_tokens
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http


if typing.TYPE_CHECKING:
    from mergify_engine import worker

SUMMARY_SHA_EXPIRATION = 60 * 60 * 24 * 31 * 1  # 1 Month


class MergifyConfigFile(github_types.GitHubContentFile):
    decoded_content: bytes


class T_PayloadEventSource(typing.TypedDict):
    event_type: github_types.GitHubEventType
    data: github_types.GitHubEvent
    timestamp: str


@dataclasses.dataclass
class PullRequestAttributeError(AttributeError):
    name: str


@dataclasses.dataclass
class Installation:
    owner_id: github_types.GitHubAccountIdType
    owner_login: github_types.GitHubLogin
    subscription: subscription.Subscription
    client: github.AsyncGithubInstallationClient
    redis: utils.RedisCache

    repositories: "typing.Dict[github_types.GitHubRepositoryName, Repository]" = (
        dataclasses.field(default_factory=dict)
    )
    _user_tokens: typing.Optional[user_tokens.UserTokens] = None

    @property
    def stream_name(self) -> "worker.StreamNameType":
        return typing.cast(
            "worker.StreamNameType", f"stream~{self.owner_login}~{self.owner_id}"
        )

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
                self.redis, self.owner_id
            )
        return self._user_tokens

    async def get_pull_request_context(
        self,
        repo_id: github_types.GitHubRepositoryIdType,
        pull_number: github_types.GitHubPullRequestNumber,
    ) -> "Context":
        for repository in self.repositories.values():
            if repository.repo["id"] == repo_id:
                return await repository.get_pull_request_context(pull_number)

        pull = await self.client.item(f"/repositories/{repo_id}/pulls/{pull_number}")
        repository = self.get_repository_from_github_data(pull["base"]["repo"])
        return await repository.get_pull_request_context(pull_number, pull)

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
        redis: utils.RedisCache,
        owner: github_types.GitHubAccount,
        team_slug: github_types.GitHubTeamSlug,
    ) -> None:
        await redis.hdel(
            cls._team_members_cache_key_for_repo(owner["id"]),
            team_slug,
        )

    @classmethod
    async def clear_team_members_cache_for_org(
        cls, redis: utils.RedisCache, user: github_types.GitHubAccount
    ) -> None:
        await redis.delete(cls._team_members_cache_key_for_repo(user["id"]))

    async def get_team_members(
        self, team_slug: github_types.GitHubTeamSlug
    ) -> typing.List[github_types.GitHubLogin]:

        key = self._team_members_cache_key_for_repo(self.owner_id)
        members_raw = await self.redis.hget(key, team_slug)
        if members_raw is None:
            members = [
                github_types.GitHubLogin(member["login"])
                async for member in self.client.items(
                    f"/orgs/{self.owner_login}/teams/{team_slug}/members"
                )
            ]
            # TODO(sileht): move to msgpack when we remove redis-cache connection
            # from decode_responses=True (eg: MRGFY-285)
            pipe = await self.redis.pipeline()
            await pipe.hset(key, team_slug, json.dumps(members))
            await pipe.expire(key, self.TEAM_MEMBERS_EXPIRATION)
            await pipe.execute()
            return members
        else:
            return typing.cast(
                typing.List[github_types.GitHubLogin], json.loads(members_raw)
            )


class RepositoryCache(typing.TypedDict, total=False):
    mergify_config: typing.Optional[MergifyConfigFile]
    branches: typing.Dict[github_types.GitHubRefType, github_types.GitHubBranch]


@dataclasses.dataclass
class Repository(object):
    installation: Installation
    repo: github_types.GitHubRepository
    pull_contexts: "typing.Dict[github_types.GitHubPullRequestNumber, Context]" = (
        dataclasses.field(default_factory=dict)
    )

    # FIXME(sileht): https://github.com/python/mypy/issues/5723
    _cache: RepositoryCache = dataclasses.field(default_factory=RepositoryCache)  # type: ignore

    @property
    def base_url(self) -> str:
        """The URL prefix to make GitHub request."""
        return f"/repos/{self.installation.owner_login}/{self.repo['name']}"

    MERGIFY_CONFIG_FILENAMES = [
        ".mergify.yml",
        ".mergify/config.yml",
        ".github/mergify.yml",
    ]

    @staticmethod
    def get_config_location_cache_key(
        owner_login: github_types.GitHubLogin,
        repo_name: github_types.GitHubRepositoryName,
    ) -> str:
        return f"config-location~{owner_login}~{repo_name}"

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

        filenames = self.MERGIFY_CONFIG_FILENAMES.copy()
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

            yield MergifyConfigFile(
                type=content["type"],
                content=content["content"],
                path=content["path"],
                sha=content["sha"],
                decoded_content=base64.b64decode(
                    bytearray(content["content"], "utf-8")
                ),
            )

    async def get_mergify_config_file(self) -> typing.Optional[MergifyConfigFile]:
        if "mergify_config" in self._cache:
            return self._cache["mergify_config"]

        config_location_cache = self.get_config_location_cache_key(
            self.installation.owner_login, self.repo["name"]
        )
        cached_filename = await self.installation.redis.get(config_location_cache)
        async for config_file in self.iter_mergify_config_files(
            preferred_filename=cached_filename
        ):
            if cached_filename != config_file["path"]:
                await self.installation.redis.set(
                    config_location_cache, config_file["path"], ex=60 * 60 * 24 * 31
                )
            self._cache["mergify_config"] = config_file
            return config_file

        await self.installation.redis.delete(config_location_cache)
        self._cache["mergify_config"] = None
        return None

    async def get_branch(
        self,
        branch_name: github_types.GitHubRefType,
    ) -> github_types.GitHubBranch:
        branches = self._cache.setdefault("branches", {})
        if branch_name not in branches:
            escaped_branch_name = parse.quote(branch_name, safe="")
            branches[branch_name] = typing.cast(
                github_types.GitHubBranch,
                await self.installation.client.item(
                    f"{self.base_url}/branches/{escaped_branch_name}"
                ),
            )
        return branches[branch_name]

    async def get_pull_request_context(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
        pull: typing.Optional[github_types.GitHubPullRequest] = None,
    ) -> "Context":
        if pull_number not in self.pull_contexts:
            if pull is None:
                pull = await self.installation.client.item(
                    f"{self.base_url}/pulls/{pull_number}"
                )
            elif pull["number"] != pull_number:
                raise RuntimeError(
                    'get_pull_request_context() needs pull["number"] == pull_number'
                )
            ctxt = await Context.create(self, pull)
            self.pull_contexts[pull_number] = ctxt

        return self.pull_contexts[pull_number]

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
        redis: utils.RedisCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
        user: github_types.GitHubAccount,
    ) -> None:
        await redis.hdel(
            cls._users_permission_cache_key_for_repo(owner["id"], repo["id"]),
            user["id"],
        )

    @classmethod
    async def clear_user_permission_cache_for_repo(
        cls,
        redis: utils.RedisCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
    ) -> None:
        await redis.delete(
            cls._users_permission_cache_key_for_repo(owner["id"], repo["id"])
        )

    @classmethod
    async def clear_user_permission_cache_for_org(
        cls, redis: utils.RedisCache, user: github_types.GitHubAccount
    ) -> None:
        pipeline = await redis.pipeline()
        async for key in redis.scan_iter(
            f"{cls.USERS_PERMISSION_CACHE_KEY_PREFIX}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{user['id']}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}*",
            count=10000,
        ):
            await pipeline.delete(key)
        await pipeline.execute()

    async def has_write_permission(self, user: github_types.GitHubAccount) -> bool:
        key = self._users_permission_cache_key
        permission = await self.installation.redis.hget(key, user["id"])
        if permission is None:
            permission = (
                await self.installation.client.item(
                    f"{self.base_url}/collaborators/{user['login']}/permission"
                )
            )["permission"]
            pipe = await self.installation.redis.pipeline()
            await pipe.hset(key, user["id"], permission)
            await pipe.expire(key, self.USERS_PERMISSION_EXPIRATION)
            await pipe.execute()

        return permission in (
            "admin",
            "maintain",
            "write",
        )

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
        redis: utils.RedisCache,
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
        redis: utils.RedisCache,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
    ) -> None:
        await redis.delete(
            cls._teams_permission_cache_key_for_repo(owner["id"], repo["id"])
        )

    @classmethod
    async def clear_team_permission_cache_for_org(
        cls, redis: utils.RedisCache, org: github_types.GitHubAccount
    ) -> None:
        pipeline = await redis.pipeline()
        async for key in redis.scan_iter(
            f"{cls.TEAMS_PERMISSION_CACHE_KEY_PREFIX}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}{org['id']}{cls.TEAMS_PERMISSION_CACHE_KEY_DELIMITER}*",
            count=10000,
        ):
            await pipeline.delete(key)
        await pipeline.execute()

    async def team_has_read_permission(self, team: github_types.GitHubTeamSlug) -> bool:
        key = self._teams_permission_cache_key
        read_permission_raw = await self.installation.redis.hget(key, team)
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
            pipe = await self.installation.redis.pipeline()
            await pipe.hset(key, team, str(int(read_permission)))
            await pipe.expire(key, self.TEAMS_PERMISSION_EXPIRATION)
            await pipe.execute()
        else:
            read_permission = bool(int(read_permission_raw))

        return read_permission

    async def get_branch_protection_checks(
        self,
        branch_name: github_types.GitHubRefType,
    ) -> typing.List[str]:
        try:
            branch = await self.get_branch(branch_name)
        except http.HTTPNotFound:
            return []
        if not branch["protection"]["enabled"]:
            return []
        return branch["protection"]["required_status_checks"]["contexts"]


class ContextCache(typing.TypedDict, total=False):
    consolidated_reviews: typing.Tuple[
        typing.List[github_types.GitHubReview],
        typing.List[github_types.GitHubReview],
    ]
    pull_check_runs: typing.List[github_types.GitHubCheckRun]
    pull_statuses: typing.List[github_types.GitHubStatus]
    reviews: typing.List[github_types.GitHubReview]
    is_behind: bool
    files: typing.List[github_types.GitHubFile]
    commits: typing.List[github_types.GitHubBranchCommit]


ContextAttributeType = typing.Union[
    bool,
    typing.List[str],
    str,
    int,
    datetime.time,
    date.PartialDatetime,
    datetime.datetime,
]


@dataclasses.dataclass
class Context(object):
    repository: Repository
    pull: github_types.GitHubPullRequest
    sources: typing.List[T_PayloadEventSource] = dataclasses.field(default_factory=list)
    configuration_changed: bool = False
    pull_request: "PullRequest" = dataclasses.field(init=False)
    log: logging.LoggerAdapter = dataclasses.field(init=False)

    # FIXME(sileht): https://github.com/python/mypy/issues/5723
    _cache: ContextCache = dataclasses.field(default_factory=ContextCache)  # type: ignore

    SUMMARY_NAME = "Summary"

    @property
    def redis(self) -> utils.RedisCache:
        # TODO(sileht): remove me when context split if done
        return self.repository.installation.redis

    @property
    def subscription(self) -> subscription.Subscription:
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

    @classmethod
    async def create(
        cls,
        repository: Repository,
        pull: github_types.GitHubPullRequest,
        sources: typing.Optional[typing.List[T_PayloadEventSource]] = None,
    ) -> "Context":
        if sources is None:
            sources = []
        self = cls(repository, pull, sources)
        await self._ensure_complete()
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
            gh_pull_url=self.pull.get("html_url", "<unknown-yet>"),
            gh_pull_state=(
                "merged"
                if self.pull.get("merged")
                else (self.pull.get("mergeable_state", "unknown") or "none")
            ),
        )
        return self

    async def set_summary_check(
        self, result: check_api.Result
    ) -> github_types.GitHubCheckRun:
        """Set the Mergify Summary check result."""

        previous_sha = await self.get_cached_last_summary_head_sha()
        # NOTE(sileht): we first commit in redis the future sha,
        # so engine.create_initial_summary() cannot creates a second SUMMARY
        await self._save_cached_last_summary_head_sha(self.pull["head"]["sha"])

        try:
            return await check_api.set_check_run(self, self.SUMMARY_NAME, result)
        except Exception:
            if previous_sha:
                # Restore previous sha in redis
                await self._save_cached_last_summary_head_sha(previous_sha)
            raise

    @staticmethod
    def redis_last_summary_head_sha_key(pull: github_types.GitHubPullRequest) -> str:
        owner = pull["base"]["repo"]["owner"]["id"]
        repo = pull["base"]["repo"]["id"]
        pull_number = pull["number"]
        return f"summary-sha~{owner}~{repo}~{pull_number}"

    @classmethod
    async def get_cached_last_summary_head_sha_from_pull(
        cls,
        redis_cache: utils.RedisCache,
        pull: github_types.GitHubPullRequest,
    ) -> typing.Optional[github_types.SHAType]:
        return typing.cast(
            typing.Optional[github_types.SHAType],
            await redis_cache.get(cls.redis_last_summary_head_sha_key(pull)),
        )

    async def get_cached_last_summary_head_sha(
        self,
    ) -> typing.Optional[github_types.SHAType]:
        return await self.get_cached_last_summary_head_sha_from_pull(
            self.redis,
            self.pull,
        )

    async def clear_cached_last_summary_head_sha(self) -> None:
        await self.redis.delete(self.redis_last_summary_head_sha_key(self.pull))

    async def _save_cached_last_summary_head_sha(
        self, sha: github_types.SHAType
    ) -> None:
        # NOTE(sileht): We store it only for 1 month, if we lose it it's not a big deal, as it's just
        # to avoid race conditions when too many synchronize events occur in a short period of time
        await self.redis.set(
            self.redis_last_summary_head_sha_key(self.pull),
            sha,
            ex=SUMMARY_SHA_EXPIRATION,
        )

    async def consolidated_reviews(
        self,
    ) -> typing.Tuple[
        typing.List[github_types.GitHubReview], typing.List[github_types.GitHubReview]
    ]:
        if "consolidated_reviews" in self._cache:
            return self._cache["consolidated_reviews"]

        # Ignore reviews that are not from someone with admin/write permissions
        # And only keep the last review for each user.
        comments: typing.Dict[github_types.GitHubLogin, github_types.GitHubReview] = {}
        approvals: typing.Dict[github_types.GitHubLogin, github_types.GitHubReview] = {}
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

        self._cache["consolidated_reviews"] = list(comments.values()), list(
            approvals.values()
        )
        return self._cache["consolidated_reviews"]

    async def _get_consolidated_data(self, name: str) -> ContextAttributeType:
        if name == "assignee":
            return [a["login"] for a in self.pull["assignees"]]

        elif name == "label":
            return [label["name"] for label in self.pull["labels"]]

        elif name == "review-requested":
            return [
                typing.cast(str, u["login"]) for u in self.pull["requested_reviewers"]
            ] + ["@" + t["slug"] for t in self.pull["requested_teams"]]

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
            return self.pull["state"] == "closed"

        elif name == "milestone":
            return (
                self.pull["milestone"]["title"]
                if self.pull["milestone"] is not None
                else ""
            )

        elif name == "number":
            return typing.cast(int, self.pull["number"])

        elif name == "conflict":
            return self.pull["mergeable_state"] == "dirty"

        elif name == "base":
            return self.pull["base"]["ref"]

        elif name == "head":
            return self.pull["head"]["ref"]

        elif name == "locked":
            return self.pull["locked"]

        elif name == "title":
            return self.pull["title"]

        elif name == "body":
            return self.pull["body"]

        elif name == "files":
            return [f["filename"] for f in await self.files]

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
                if state in ["failure", "action_required", "cancelled", "timed_out"]
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
        elif name == "current-datetime":
            return utils.utcnow()
        elif name == "current-time":
            return utils.utcnow().timetz()
        elif name == "current-day":
            return date.Day(utils.utcnow().day)
        elif name == "current-month":
            return date.Month(utils.utcnow().month)
        elif name == "current-year":
            return date.Year(utils.utcnow().year)
        elif name == "current-day-of-week":
            return date.DayOfWeek(utils.utcnow().isoweekday())
        elif name == "updated-at":
            return date.fromisoformat(self.pull["updated_at"])
        else:
            raise PullRequestAttributeError(name)

    DEPENDS_ON = re.compile(
        r"^ *Depends-On: +(?:#|"
        + config.GITHUB_URL
        + r"/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/)(?P<pull>\d+) *$",
        re.MULTILINE | re.IGNORECASE,
    )

    def get_depends_on(self) -> typing.Set[github_types.GitHubPullRequestNumber]:
        if not self.pull["body"]:
            return set()
        return {
            github_types.GitHubPullRequestNumber(int(pull))
            for owner, repo, pull in self.DEPENDS_ON.findall(self.pull["body"])
            if (owner == "" and repo == "")
            or (
                owner == self.pull["base"]["user"]["login"]
                and repo == self.pull["base"]["repo"]["name"]
            )
        }

    async def update_pull_check_runs(self, check: github_types.GitHubCheckRun) -> None:
        self._cache["pull_check_runs"] = [
            c for c in await self.pull_check_runs if c["name"] != check["name"]
        ]
        self._cache["pull_check_runs"].append(check)

    @property
    async def pull_check_runs(self) -> typing.List[github_types.GitHubCheckRun]:
        if "pull_check_runs" in self._cache:
            return self._cache["pull_check_runs"]

        checks = await check_api.get_checks_for_ref(self, self.pull["head"]["sha"])
        self._cache["pull_check_runs"] = checks
        return checks

    @property
    async def pull_engine_check_runs(self) -> typing.List[github_types.GitHubCheckRun]:
        return [
            c
            for c in await self.pull_check_runs
            if c["app"]["id"] == config.INTEGRATION_ID
        ]

    async def get_engine_check_run(
        self, name: str
    ) -> typing.Optional[github_types.GitHubCheckRun]:
        return first.first(
            await self.pull_engine_check_runs, key=lambda c: c["name"] == name
        )

    @property
    async def pull_statuses(self) -> typing.List[github_types.GitHubStatus]:
        if "pull_statuses" in self._cache:
            return self._cache["pull_statuses"]

        statuses = [
            s
            async for s in typing.cast(
                typing.AsyncIterable[github_types.GitHubStatus],
                self.client.items(
                    f"{self.base_url}/commits/{self.pull['head']['sha']}/status",
                    list_items="statuses",
                ),
            )
        ]
        self._cache["pull_statuses"] = statuses
        return statuses

    @property
    async def checks(self):
        # NOTE(sileht): check-runs are returned in reverse chronogical order,
        # so if it has ran twice we must keep only the more recent
        # statuses are good as GitHub already ensures the uniqueness of the name

        # First put all branch protections checks as pending and then override with
        # the real status
        checks = {
            context: "pending"
            for context in await self.repository.get_branch_protection_checks(
                self.pull["base"]["ref"]
            )
        }
        # NOTE(sileht): conclusion can be one of success, failure, neutral,
        # cancelled, timed_out, or action_required, and  None for "pending"
        checks.update(
            {c["name"]: c["conclusion"] for c in reversed(await self.pull_check_runs)}
        )
        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        checks.update({s["context"]: s["state"] for s in await self.pull_statuses})
        return checks

    UNUSABLE_STATES = ["unknown", None]

    # NOTE(sileht): quickly retry, if we don't get the status on time
    # the exception is recatch in worker.py, so worker will retry it later
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),  # type: ignore[no-untyped-call]
        stop=tenacity.stop_after_attempt(5),  # type: ignore[no-untyped-call]
        retry=tenacity.retry_if_exception_type(exceptions.MergeableStateUnknown),  # type: ignore[no-untyped-call]
        reraise=True,
    )
    async def _ensure_complete(self) -> None:
        if not (
            self._is_data_complete()
            and self._is_background_github_processing_completed()
        ):
            self.pull = await self.client.item(
                f"{self.base_url}/pulls/{self.pull['number']}"
            )

        if self._is_background_github_processing_completed():
            return

        raise exceptions.MergeableStateUnknown(self)

    def _is_data_complete(self) -> bool:
        # NOTE(sileht): If pull request come from /pulls listing or check-runs sometimes,
        # they are incomplete, This ensure we have the complete view
        fields_to_control = (
            "state",
            "mergeable_state",
            "merged_by",
            "merged",
            "merged_at",
        )
        for field in fields_to_control:
            if field not in self.pull:
                return False
        return True

    def _is_background_github_processing_completed(self) -> bool:
        return (
            self.pull["state"] == "closed"
            or self.pull["mergeable_state"] not in self.UNUSABLE_STATES
        )

    async def update(self) -> None:
        # TODO(sileht): Remove me,
        # Don't use it, because consolidated data are not updated after that.
        # Only used by merge action for posting an update report after rebase.
        self.pull = await self.client.item(
            f"{self.base_url}/pulls/{self.pull['number']}"
        )

        try:
            del self._cache["pull_check_runs"]
        except KeyError:
            pass

    @property
    async def is_behind(self) -> bool:
        if "is_behind" in self._cache:
            return self._cache["is_behind"]

        branch_name_escaped = parse.quote(self.pull["base"]["ref"], safe="")
        branch = typing.cast(
            github_types.GitHubBranch,
            await self.client.item(f"{self.base_url}/branches/{branch_name_escaped}"),
        )
        for commit in await self.commits:
            for parent in commit["parents"]:
                if parent["sha"] == branch["commit"]["sha"]:
                    self._cache["is_behind"] = False
                    return False

        self._cache["is_behind"] = True
        return True

    def is_merge_queue_pr(self) -> bool:
        return self.pull["user"]["id"] == config.BOT_USER_ID and self.pull["head"][
            "ref"
        ].startswith(constants.MERGE_QUEUE_BRANCH_PREFIX)

    def has_been_synchronized(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, source["data"])
                if (
                    event["action"] == "synchronize"
                    and event["sender"]["id"] != config.BOT_USER_ID
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
        pr_state = (
            "merged"
            if self.pull["merged"]
            else (self.pull["mergeable_state"] or "none")
        )
        return f"{login}/{repo}/pull/{number}@{branch} s:{pr_state}"

    @property
    async def reviews(self) -> typing.List[github_types.GitHubReview]:
        if "reviews" in self._cache:
            return self._cache["reviews"]

        reviews = [
            review
            async for review in typing.cast(
                typing.AsyncIterable[github_types.GitHubReview],
                self.client.items(
                    f"{self.base_url}/pulls/{self.pull['number']}/reviews"
                ),
            )
        ]
        self._cache["reviews"] = reviews
        return reviews

    @property
    async def commits(self) -> typing.List[github_types.GitHubBranchCommit]:
        if "commits" in self._cache:
            return self._cache["commits"]
        commits = [
            commit
            async for commit in typing.cast(
                typing.AsyncIterable[github_types.GitHubBranchCommit],
                self.client.items(
                    f"{self.base_url}/pulls/{self.pull['number']}/commits"
                ),
            )
        ]
        self._cache["commits"] = commits
        return commits

    @property
    async def files(self) -> typing.List[github_types.GitHubFile]:
        if "files" in self._cache:
            return self._cache["files"]
        files = [
            file
            async for file in typing.cast(
                typing.AsyncIterable[github_types.GitHubFile],
                self.client.items(
                    f"{self.base_url}/pulls/{self.pull['number']}/files?per_page=100"
                ),
            )
        ]
        self._cache["files"] = files
        return files

    @property
    def pull_from_fork(self) -> bool:
        return self.pull["head"]["repo"]["id"] != self.pull["base"]["repo"]["id"]

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

    def __str__(self):
        return self.message


@dataclasses.dataclass
class PullRequest:
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
        "base",
        "head",
        "locked",
        "title",
        "body",
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
        "files",
    }

    async def __getattr__(self, name: str) -> ContextAttributeType:
        return await self.context._get_consolidated_data(name.replace("_", "-"))

    def __iter__(self):
        return iter(self.ATTRIBUTES | self.LIST_ATTRIBUTES)

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
    ) -> str:
        """Render a template interpolating variables based on pull request attributes."""
        env = jinja2.sandbox.SandboxedEnvironment(
            undefined=jinja2.StrictUndefined,
        )
        with self._template_exceptions_mapping():
            used_variables = jinja2.meta.find_undeclared_variables(env.parse(template))
            infos = {}
            for k in used_variables:
                if extra_variables and k in extra_variables:
                    infos[k] = extra_variables[k]
                else:
                    infos[k] = await getattr(self, k)
            return env.from_string(template).render(**infos)

    @staticmethod
    @contextlib.contextmanager
    def _template_exceptions_mapping():
        try:
            yield
        except jinja2.exceptions.TemplateSyntaxError as tse:
            raise RenderTemplateFailure(tse.message, tse.lineno)
        except jinja2.exceptions.TemplateError as te:
            raise RenderTemplateFailure(te.message)
        except PullRequestAttributeError as e:
            raise RenderTemplateFailure(f"Unknown pull request attribute: {e.name}")
