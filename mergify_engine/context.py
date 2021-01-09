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
import dataclasses
import functools
import itertools
import logging
import typing
from urllib import parse

import aredis
import daiquiri
import jinja2.exceptions
import jinja2.runtime
import jinja2.sandbox
import jinja2.utils
import tenacity

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http


SUMMARY_SHA_EXPIRATION = 60 * 60 * 24 * 31  # ~ 1 Month


class T_PayloadEventSource(typing.TypedDict):
    event_type: github_types.GitHubEventType
    data: github_types.GitHubEvent
    timestamp: str


@dataclasses.dataclass
class PullRequestAttributeError(AttributeError):
    name: str


@dataclasses.dataclass
class Context(object):
    client: github.GithubInstallationClient
    pull: github_types.GitHubPullRequest
    subscription: subscription.Subscription
    sources: typing.List[T_PayloadEventSource] = dataclasses.field(default_factory=list)
    pull_request: "PullRequest" = dataclasses.field(init=False)
    log: logging.LoggerAdapter = dataclasses.field(init=False)

    SUMMARY_NAME = "Summary"
    USER_PERMISSION_EXPIRATION = 3600  # 1 hour

    def __post_init__(self):
        self._ensure_complete()

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

    @property
    def base_url(self):
        """The URL prefix to make GitHub request."""
        return f"/repos/{self.pull['base']['user']['login']}/{self.pull['base']['repo']['name']}"

    USERS_PERMISSION_CACHE_KEY_PREFIX = "users_permission"
    USERS_PERMISSION_CACHE_KEY_DELIMITER = "/"

    @classmethod
    def _users_permission_cache_key_for_repo(
        cls, owner: github_types.GitHubAccount, repo: github_types.GitHubRepository
    ) -> str:
        return f"{cls.USERS_PERMISSION_CACHE_KEY_PREFIX}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{owner['id']}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{repo['id']}"

    @property
    def _users_permission_cache_key(self) -> str:
        return self._users_permission_cache_key_for_repo(
            self.pull["base"]["user"], self.pull["base"]["repo"]
        )

    @classmethod
    async def clear_user_permission_cache_for_user(
        cls,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
        user: github_types.GitHubAccount,
    ) -> None:
        redis = await utils.get_aredis_for_cache()
        await redis.hdel(
            cls._users_permission_cache_key_for_repo(owner, repo), user["id"]
        )

    @classmethod
    def clear_user_permission_cache_for_repo(
        cls,
        owner: github_types.GitHubAccount,
        repo: github_types.GitHubRepository,
    ) -> None:
        with utils.get_redis_for_cache() as redis:  # type: ignore
            redis.delete(cls._users_permission_cache_key_for_repo(owner, repo))

    @classmethod
    def clear_user_permission_cache_for_org(
        cls, user: github_types.GitHubAccount
    ) -> None:
        with utils.get_redis_for_cache() as redis:  # type: ignore
            for key in redis.scan_iter(
                f"{cls.USERS_PERMISSION_CACHE_KEY_PREFIX}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}{user['id']}{cls.USERS_PERMISSION_CACHE_KEY_DELIMITER}*"
            ):
                redis.delete(key)

    def has_write_permission(self, user: github_types.GitHubAccount) -> bool:
        with utils.get_redis_for_cache() as redis:  # type: ignore
            key = self._users_permission_cache_key
            permission = redis.hget(key, user["id"])
            if permission is None:
                permission = self.client.item(
                    f"{self.base_url}/collaborators/{user['login']}/permission"
                )["permission"]
                with redis.pipeline() as pipe:
                    pipe.hset(key, user["id"], permission)
                    pipe.expire(key, self.USER_PERMISSION_EXPIRATION)
                    pipe.execute()

        return permission in (
            "admin",
            "maintain",
            "write",
        )

    async def set_summary_check(
        self, result: check_api.Result
    ) -> github_types.GitHubCheckRun:
        """Set the Mergify Summary check result."""

        previous_sha = await self.get_cached_last_summary_head_sha()
        # NOTE(sileht): we first commit in redis the future sha,
        # so engine.create_initial_summary() cannot creates a second SUMMARY
        self._save_cached_last_summary_head_sha(self.pull["head"]["sha"])

        try:
            return check_api.set_check_run(self, self.SUMMARY_NAME, result)
        except Exception:
            if previous_sha:
                # Restore previous sha in redis
                self._save_cached_last_summary_head_sha(previous_sha)
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
        redis_cache: aredis.StrictRedis,
        pull: github_types.GitHubPullRequest,
    ) -> typing.Optional[github_types.SHAType]:
        return typing.cast(
            typing.Optional[github_types.SHAType],
            await redis_cache.get(cls.redis_last_summary_head_sha_key(pull)),
        )

    async def get_cached_last_summary_head_sha(
        self,
    ) -> typing.Optional[github_types.SHAType]:
        redis_cache = await utils.get_aredis_for_cache()
        return await self.get_cached_last_summary_head_sha_from_pull(
            redis_cache,
            self.pull,
        )

    def clear_cached_last_summary_head_sha(self) -> None:
        with utils.get_redis_for_cache() as redis:  # type: ignore[attr-defined]
            redis.delete(self.redis_last_summary_head_sha_key(self.pull))

    def _save_cached_last_summary_head_sha(self, sha: github_types.SHAType) -> None:
        # NOTE(sileht): We store it only for 1 month, if we lose it it's not a big deal, as it's just
        # to avoid race conditions when too many synchronize events occur in a short period of time
        with utils.get_redis_for_cache() as redis:  # type: ignore[attr-defined]
            redis.set(
                self.redis_last_summary_head_sha_key(self.pull),
                sha,
                ex=SUMMARY_SHA_EXPIRATION,
            )

    def _get_valid_user_ids(self) -> typing.Set[github_types.GitHubAccountIdType]:
        return {
            r["user"]["id"]
            for r in self.reviews
            if (
                r["user"] is not None
                and (r["user"]["type"] == "Bot" or self.has_write_permission(r["user"]))
            )
        }

    @functools.cached_property
    def consolidated_reviews(
        self,
    ) -> typing.Tuple[
        typing.List[github_types.GitHubReview], typing.List[github_types.GitHubReview]
    ]:
        # Ignore reviews that are not from someone with admin/write permissions
        # And only keep the last review for each user.
        comments = dict()
        approvals = dict()
        valid_user_ids = self._get_valid_user_ids()
        for review in self.reviews:
            if not review["user"] or review["user"]["id"] not in valid_user_ids:
                continue
            # Only keep latest review of an user
            if review["state"] == "COMMENTED":
                comments[review["user"]["login"]] = review
            else:
                approvals[review["user"]["login"]] = review
        return list(comments.values()), list(approvals.values())

    def _get_consolidated_data(self, name):
        if name == "assignee":
            return [a["login"] for a in self.pull["assignees"]]

        elif name == "label":
            return [label["name"] for label in self.pull["labels"]]

        elif name == "review-requested":
            return [u["login"] for u in self.pull["requested_reviewers"]] + [
                "@" + t["slug"] for t in self.pull["requested_teams"]
            ]

        elif name == "draft":
            return self.pull["draft"]

        elif name == "author":
            return self.pull["user"]["login"]

        elif name == "merged-by":
            return self.pull["merged_by"]["login"] if self.pull["merged_by"] else ""

        elif name == "merged":
            return self.pull["merged"]

        elif name == "closed":
            return self.pull["state"] == "closed"

        elif name == "milestone":
            return self.pull["milestone"]["title"] if self.pull["milestone"] else ""

        elif name == "number":
            return self.pull["number"]

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
            return [f["filename"] for f in self.files]

        elif name == "approved-reviews-by":
            _, approvals = self.consolidated_reviews
            return [r["user"]["login"] for r in approvals if r["state"] == "APPROVED"]
        elif name == "dismissed-reviews-by":
            _, approvals = self.consolidated_reviews
            return [r["user"]["login"] for r in approvals if r["state"] == "DISMISSED"]
        elif name == "changes-requested-reviews-by":
            _, approvals = self.consolidated_reviews
            return [
                r["user"]["login"]
                for r in approvals
                if r["state"] == "CHANGES_REQUESTED"
            ]
        elif name == "commented-reviews-by":
            comments, _ = self.consolidated_reviews
            return [r["user"]["login"] for r in comments if r["state"] == "COMMENTED"]

        # NOTE(jd) The Check API set conclusion to None for pending.
        # NOTE(sileht): "pending" statuses are not really trackable, we
        # voluntary drop this event because CIs just sent they status every
        # minutes until the CI pass (at least Travis and Circle CI does
        # that). This was causing a big load on Mergify for nothing useful
        # tracked, and on big projects it can reach the rate limit very
        # quickly.
        # NOTE(sileht): Not handled for now: cancelled, timed_out, or action_required
        elif name in ("status-success", "check-success"):
            return [ctxt for ctxt, state in self.checks.items() if state == "success"]
        elif name in ("status-failure", "check-failure"):
            return [ctxt for ctxt, state in self.checks.items() if state == "failure"]
        elif name in ("status-neutral", "check-neutral"):
            return [ctxt for ctxt, state in self.checks.items() if state == "neutral"]

        else:
            raise PullRequestAttributeError(name)

    def update_pull_check_runs(self, check):
        self.pull_check_runs = [
            c for c in self.pull_check_runs if c["name"] != check["name"]
        ]
        self.pull_check_runs.append(check)

    @functools.cached_property
    def pull_check_runs(self) -> typing.List[github_types.GitHubCheckRun]:
        return check_api.get_checks_for_ref(self, self.pull["head"]["sha"])

    @property
    def pull_engine_check_runs(self) -> typing.List[github_types.GitHubCheckRun]:
        return [
            c for c in self.pull_check_runs if c["app"]["id"] == config.INTEGRATION_ID
        ]

    @functools.cached_property
    def checks(self):
        # NOTE(sileht): conclusion can be one of success, failure, neutral,
        # cancelled, timed_out, or action_required, and  None for "pending"
        checks = dict((c["name"], c["conclusion"]) for c in self.pull_check_runs)
        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        checks.update(
            (s["context"], s["state"])
            for s in self.client.items(
                f"{self.base_url}/commits/{self.pull['head']['sha']}/status",
                list_items="statuses",
            )
        )
        return checks

    def _resolve_login(self, name):
        if not name:
            return []
        elif not isinstance(name, str):
            return [name]
        elif name[0] != "@":
            return [name]

        if "/" in name:
            organization, _, team_slug = name.partition("/")
            if not team_slug or "/" in team_slug:
                # Not a team slug
                return [name]
            organization = organization[1:]
        else:
            organization = self.pull["base"]["repo"]["owner"]["login"]
            team_slug = name[1:]

        try:
            return [
                member["login"]
                for member in self.client.items(
                    f"/orgs/{organization}/teams/{team_slug}/members"
                )
            ]
        except http.HTTPClientSideError as e:
            self.log.warning(
                "fail to get the organization, team or members",
                team=name,
                status_code=e.status_code,
                detail=e.message,
            )
        return [name]

    def resolve_teams(self, values):
        if not values:
            return []
        if not isinstance(values, (list, tuple)):
            values = [values]
        values = list(itertools.chain.from_iterable((map(self._resolve_login, values))))
        return values

    UNUSABLE_STATES = ["unknown", None]

    # NOTE(sileht): quickly retry, if we don't get the status on time
    # the exception is recatch in worker.py, so worker will retry it later
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_exception_type(exceptions.MergeableStateUnknown),
        reraise=True,
    )
    def _ensure_complete(self):
        if not (
            self._is_data_complete()
            and self._is_background_github_processing_completed()
        ):
            self.pull = self.client.item(f"{self.base_url}/pulls/{self.pull['number']}")

        if self._is_background_github_processing_completed():
            return

        raise exceptions.MergeableStateUnknown(self)

    def _is_data_complete(self):
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

    def _is_background_github_processing_completed(self):
        return (
            self.pull["state"] == "closed"
            or self.pull["mergeable_state"] not in self.UNUSABLE_STATES
        )

    def update(self):
        # TODO(sileht): Remove me,
        # Don't use it, because consolidated data are not updated after that.
        # Only used by merge action for posting an update report after rebase.
        self.pull = self.client.item(f"{self.base_url}/pulls/{self.pull['number']}")
        try:
            del self.__dict__["pull_check_runs"]
        except KeyError:
            pass

    @functools.cached_property
    def is_behind(self) -> bool:
        branch_name_escaped = parse.quote(self.pull["base"]["ref"], safe="")
        branch = typing.cast(
            github_types.GitHubBranch,
            self.client.item(f"{self.base_url}/branches/{branch_name_escaped}"),
        )
        for commit in self.commits:
            for parent in commit["parents"]:
                if parent["sha"] == branch["commit"]["sha"]:
                    return False
        return True

    def have_been_synchronized(self) -> bool:
        for source in self.sources:
            if source["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, source["data"])
                if (
                    event["action"] == "synchronize"
                    and event["sender"]["id"] != config.BOT_USER_ID
                ):
                    return True
        return False

    def __str__(self) -> str:
        return "%(login)s/%(repo)s/pull/%(number)d@%(branch)s " "s:%(pr_state)s" % {
            "login": self.pull["base"]["user"]["login"],
            "repo": self.pull["base"]["repo"]["name"],
            "number": self.pull["number"],
            "branch": self.pull["base"]["ref"],
            "pr_state": (
                "merged"
                if self.pull["merged"]
                else (self.pull["mergeable_state"] or "none")
            ),
        }

    @functools.cached_property
    def reviews(self) -> typing.List[github_types.GitHubReview]:
        return list(
            self.client.items(f"{self.base_url}/pulls/{self.pull['number']}/reviews")
        )

    @functools.cached_property
    def commits(self):
        return list(
            self.client.items(f"{self.base_url}/pulls/{self.pull['number']}/commits")
        )

    @functools.cached_property
    def files(self):
        return list(
            self.client.items(f"{self.base_url}/pulls/{self.pull['number']}/files")
        )

    @property
    def pull_from_fork(self):
        return self.pull["head"]["repo"]["id"] != self.pull["base"]["repo"]["id"]

    def github_workflow_changed(self):
        for f in self.files:
            if f["filename"].startswith(".github/workflows"):
                return True
        return False


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
        "check-failure",
        "check-neutral",
        "status-success",
        "status-failure",
        "status-neutral",
        "files",
    }

    def __getattr__(self, name):
        return self.context._get_consolidated_data(name.replace("_", "-"))

    def __iter__(self):
        return iter(self.ATTRIBUTES | self.LIST_ATTRIBUTES)

    def items(self):
        for k in self:
            yield k, getattr(self, k)

    def render_template(self, template, extra_variables=None):
        """Render a template interpolating variables based on pull request attributes."""
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        PullRequestJinjaContext.inject(env, self, extra_variables)
        try:
            return env.from_string(template).render()
        except jinja2.exceptions.TemplateSyntaxError as tse:
            raise RenderTemplateFailure(tse.message, tse.lineno)
        except jinja2.exceptions.TemplateError as te:
            raise RenderTemplateFailure(te.message)
        except PullRequestAttributeError as e:
            raise RenderTemplateFailure(f"Unknown pull request attribute: {e.name}")


class PullRequestJinjaContext(jinja2.runtime.Context):
    """This is a special Context that resolves any attribute first in the "pull request" object.


    This allows to write {{author}} instead of {{pull_request.author}}."""

    _InvalidValue = object()

    def resolve_or_missing(self, key):
        if "extra_variables" in self.parent:
            try:
                return self.parent["extra_variables"][key]
            except KeyError:
                if "extra_variables" in self.vars:
                    try:
                        return self.vars["extra_variables"][key]
                    except KeyError:
                        pass

        if "pull_request" in self.parent:
            try:
                return getattr(self.parent["pull_request"], key)
            except AttributeError:
                if "pull_request" in self.vars:
                    return getattr(self.vars["pull_request"], key)
                raise

        value = super().resolve_or_missing(key)
        if value == self._InvalidValue:
            return jinja2.utils.missing

    @classmethod
    def inject(cls, env, pull_request, extra_variables=None):
        """Inject this context into a Jinja Environment."""
        # Set all the value to _InvalidValue as the PullRequestJinjaContext will resolve
        # values correctly anyway. We still need to have those entries in
        # the global dict so find_undeclared_variables works correctly.
        env.globals["pull_request"] = pull_request
        env.globals.update(
            dict((k.replace("-", "_"), cls._InvalidValue) for k in pull_request)
        )
        if extra_variables:
            env.globals["extra_variables"] = extra_variables
            env.globals.update(
                dict((k.replace("-", "_"), cls._InvalidValue) for k in extra_variables)
            )
        env.context_class = cls
