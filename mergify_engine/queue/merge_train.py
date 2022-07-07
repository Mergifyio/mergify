# -*- encoding: utf-8 -*-
#
# Copyright © 2021—2022 Mergify SAS
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
import functools
import itertools
import typing
from urllib import parse

import daiquiri
from ddtrace import tracer
import first
import pydantic
import tenacity

from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import queue
from mergify_engine import signals
from mergify_engine import utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens
from mergify_engine.queue import freeze


if typing.TYPE_CHECKING:
    from mergify_engine import rules
    from mergify_engine.rules import checks_status


def build_pr_link(
    repository: context.Repository,
    pull_request_number: github_types.GitHubPullRequestNumber,
    label: typing.Optional[str] = None,
) -> str:
    if label is None:
        label = f"#{pull_request_number}"

    return f"[{label}](/{repository.installation.owner_login}/{repository.repo['name']}/pull/{pull_request_number})"


LOG = daiquiri.getLogger(__name__)

CHECK_ASSERTS = {
    # green check mark
    "success": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/check-green-16.png",
    # red x
    "failure": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "error": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "cancelled": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "skipped": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
    "action_required": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "timed_out": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    # yellow dot
    "pending": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/dot-yellow-16.png",
    None: "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/dot-yellow-16.png",
    # grey square
    "neutral": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
    "stale": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
}


def is_base_branch_not_exists_exception(exc: BaseException) -> bool:
    return isinstance(exc, http.HTTPNotFound) and "Base does not exist" in exc.message


class UnexpectedChange:
    pass


class QueueRuleReport(typing.NamedTuple):
    name: str
    summary: str


@dataclasses.dataclass
class UnexpectedDraftPullRequestChange(UnexpectedChange):
    draft_pull_request_number: github_types.GitHubPullRequestNumber

    def __str__(self) -> str:
        return f"the draft pull request #{self.draft_pull_request_number} has been manually updated"


@dataclasses.dataclass
class UnexpectedUpdatedPullRequestChange(UnexpectedChange):
    updated_pull_request_number: github_types.GitHubPullRequestNumber

    def __str__(self) -> str:
        return f"the updated pull request #{self.updated_pull_request_number} has been manually updated"


@dataclasses.dataclass
class UnexpectedBaseBranchChange(UnexpectedChange):
    base_sha: github_types.SHAType

    def __str__(self) -> str:
        return f"an external action moved the branch head {self.base_sha}"


@dataclasses.dataclass
class TrainCarPullRequestCreationPostponed(Exception):
    car: "TrainCar"


@dataclasses.dataclass
class TrainCarPullRequestCreationFailure(Exception):
    car: "TrainCar"


class EmbarkedPullWithCar(typing.NamedTuple):
    embarked_pull: "EmbarkedPull"
    car: typing.Optional["TrainCar"]


@dataclasses.dataclass
class EmbarkedPull:
    train: "Train" = dataclasses.field(repr=False)
    user_pull_request_number: github_types.GitHubPullRequestNumber
    config: queue.PullQueueConfig
    queued_at: datetime.datetime

    class Serialized(typing.TypedDict):
        user_pull_request_number: github_types.GitHubPullRequestNumber
        config: queue.PullQueueConfig
        queued_at: datetime.datetime

    class OldSerialized(typing.NamedTuple):
        user_pull_request_number: github_types.GitHubPullRequestNumber
        config: queue.PullQueueConfig
        queued_at: datetime.datetime

    @classmethod
    def deserialize(
        cls,
        train: "Train",
        data: typing.Union["EmbarkedPull.Serialized", "EmbarkedPull.OldSerialized"],
    ) -> "EmbarkedPull":
        if isinstance(data, (tuple, list)):
            user_pull_request_number = data[0]
            config = data[1]
            queued_at = data[2]

            return cls(
                train=train,
                user_pull_request_number=user_pull_request_number,
                config=config,
                queued_at=queued_at,
            )
        else:
            return cls(
                train=train,
                user_pull_request_number=data["user_pull_request_number"],
                config=data["config"],
                queued_at=data["queued_at"],
            )

    def serialized(self) -> "EmbarkedPull.Serialized":
        return self.Serialized(
            user_pull_request_number=self.user_pull_request_number,
            config=self.config,
            queued_at=self.queued_at,
        )


TrainCarState = typing.Literal[
    "pending",
    "created",
    "updated",
    "failed",
]


CheckStateT = typing.Literal[
    "success",
    "failure",
    "error",
    "cancelled",
    "skipped",
    "action_required",
    "timed_out",
    "pending",
    "neutral",
    "stale",
]


@pydantic.dataclasses.dataclass
class QueueCheck:
    name: str = dataclasses.field(metadata={"description": "Check name"})
    description: str = dataclasses.field(metadata={"description": "Check description"})
    url: typing.Optional[str] = dataclasses.field(
        metadata={"description": "Check detail url"}
    )
    state: CheckStateT = dataclasses.field(metadata={"description": "Check state"})
    avatar_url: typing.Optional[str] = dataclasses.field(
        metadata={"description": "Check avatar_url"}
    )

    class Serialized(typing.TypedDict):
        name: str
        description: str
        url: typing.Optional[str]
        state: CheckStateT
        avatar_url: typing.Optional[str]


@dataclasses.dataclass
class DraftPullRequestCreationTemporaryFailure(Exception):
    reason: str


@dataclasses.dataclass
class TrainCar:
    train: "Train" = dataclasses.field(repr=False)
    initial_embarked_pulls: typing.List[EmbarkedPull]
    still_queued_embarked_pulls: typing.List[EmbarkedPull]
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    creation_date: datetime.datetime = dataclasses.field(default_factory=date.utcnow)
    creation_state: TrainCarState = "pending"
    checks_conclusion: check_api.Conclusion = check_api.Conclusion.PENDING
    queue_pull_request_number: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = dataclasses.field(default=None)
    failure_history: typing.List["TrainCar"] = dataclasses.field(
        default_factory=list, repr=False
    )
    head_branch: typing.Optional[str] = None
    last_checks: typing.List[QueueCheck] = dataclasses.field(default_factory=list)
    last_evaluated_conditions: typing.Optional[str] = None
    has_timed_out: bool = False
    checks_ended_timestamp: typing.Optional[datetime.datetime] = None
    ci_has_passed: bool = False

    class Serialized(typing.TypedDict):
        initial_embarked_pulls: typing.List[EmbarkedPull.Serialized]
        still_queued_embarked_pulls: typing.List[EmbarkedPull.Serialized]
        parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
        initial_current_base_sha: github_types.SHAType
        checks_conclusion: check_api.Conclusion
        creation_date: datetime.datetime
        creation_state: TrainCarState
        queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]
        # mymy can't parse recursive definition, yet
        failure_history: typing.List["TrainCar.Serialized"]  # type: ignore[misc]
        head_branch: typing.Optional[str]
        last_checks: typing.List[QueueCheck.Serialized]
        last_evaluated_conditions: typing.Optional[str]
        has_timed_out: bool
        checks_ended_timestamp: typing.Optional[datetime.datetime]
        ci_has_passed: bool

    def serialized(self) -> "TrainCar.Serialized":
        return self.Serialized(
            initial_embarked_pulls=[
                ep.serialized() for ep in self.initial_embarked_pulls
            ],
            still_queued_embarked_pulls=[
                ep.serialized() for ep in self.still_queued_embarked_pulls
            ],
            parent_pull_request_numbers=self.parent_pull_request_numbers,
            initial_current_base_sha=self.initial_current_base_sha,
            creation_date=self.creation_date,
            creation_state=self.creation_state,
            checks_conclusion=self.checks_conclusion,
            queue_pull_request_number=self.queue_pull_request_number,
            failure_history=[fh.serialized() for fh in self.failure_history],
            head_branch=self.head_branch,
            last_checks=[
                typing.cast(
                    QueueCheck.Serialized,
                    dataclasses.asdict(c),
                )
                for c in self.last_checks
            ],
            last_evaluated_conditions=self.last_evaluated_conditions,
            has_timed_out=self.has_timed_out,
            checks_ended_timestamp=self.checks_ended_timestamp,
            ci_has_passed=self.ci_has_passed,
        )

    @classmethod
    def deserialize(
        cls,
        train: "Train",
        data: "TrainCar.Serialized",
    ) -> "TrainCar":
        if "initial_embarked_pulls" in data:
            initial_embarked_pulls = [
                EmbarkedPull.deserialize(train, ep)
                for ep in data["initial_embarked_pulls"]
            ]
            still_queued_embarked_pulls = [
                EmbarkedPull.deserialize(train, ep)
                for ep in data["still_queued_embarked_pulls"]
            ]

        else:
            # old format
            initial_embarked_pulls = [
                EmbarkedPull(
                    train,
                    data["user_pull_request_number"],  # type: ignore
                    data["config"],  # type: ignore[typeddict-item]
                    data["queued_at"],  # type: ignore[typeddict-item]
                )
            ]
            still_queued_embarked_pulls = initial_embarked_pulls.copy()

        if "creation_state" in data:
            creation_state = data["creation_state"]
        else:
            creation_state = data["state"]  # type: ignore[typeddict-item]

        if "failure_history" in data:
            failure_history = [
                TrainCar.deserialize(train, fh) for fh in data["failure_history"]
            ]
        else:
            failure_history = []

        if "creation_date" in data:
            creation_date = data["creation_date"]
        else:
            creation_date = date.utcnow()

        if "last_checks" in data:
            last_checks = [QueueCheck(**c) for c in data["last_checks"]]
        else:
            last_checks = []

        if creation_state == "updated" and data["queue_pull_request_number"] is None:
            data["queue_pull_request_number"] = still_queued_embarked_pulls[
                0
            ].user_pull_request_number

        if "head_branch" not in data:
            if creation_state == "created":
                data["head_branch"] = cls._get_pulls_branch_ref(initial_embarked_pulls)
            else:
                data["head_branch"] = None

        return cls(
            train,
            initial_embarked_pulls=initial_embarked_pulls,
            still_queued_embarked_pulls=still_queued_embarked_pulls,
            parent_pull_request_numbers=data["parent_pull_request_numbers"],
            initial_current_base_sha=data["initial_current_base_sha"],
            creation_date=creation_date,
            creation_state=creation_state,
            checks_conclusion=data.get(
                "checks_conclusion", check_api.Conclusion.PENDING
            ),
            queue_pull_request_number=data["queue_pull_request_number"],
            failure_history=failure_history,
            head_branch=data["head_branch"],
            last_checks=last_checks,
            last_evaluated_conditions=data.get("last_evaluated_conditions"),
            has_timed_out=data.get("has_timed_out", False),
            checks_ended_timestamp=data.get("checks_ended_timestamp"),
            ci_has_passed=data.get("ci_has_passed", False),
        )

    def _get_user_refs(
        self,
        create_link: bool = True,
        embarked_pulls: typing.Optional[typing.List[EmbarkedPull]] = None,
    ) -> str:
        if embarked_pulls is None:
            embarked_pulls = self.initial_embarked_pulls
        refs = [
            build_pr_link(self.train.repository, ep.user_pull_request_number)
            if create_link
            else f"#{ep.user_pull_request_number}"
            for ep in embarked_pulls
        ]
        if len(refs) == 1:
            return refs[0]
        else:
            return f"[{' + '.join(refs)}]"

    def _get_embarked_refs(
        self, include_my_self: bool = True, markdown: bool = False
    ) -> str:
        if markdown:
            refs = [
                f"Branch **{self.train.ref}** ({self.initial_current_base_sha[:7]})"
            ]
        else:
            refs = [f"{self.train.ref} ({self.initial_current_base_sha[:7]})"]

        refs += [
            build_pr_link(self.train.repository, p) if markdown else f"#{p}"
            for p in self.parent_pull_request_numbers
        ]

        if include_my_self:
            return f"{', '.join(refs)} and {self._get_user_refs(create_link=markdown)}"
        elif len(refs) == 1:
            return refs[-1]
        else:
            return f"{', '.join(refs[:-1])} and {refs[-1]}"

    async def get_pull_requests_to_evaluate(
        self,
    ) -> typing.List[context.BasePullRequest]:
        if self.creation_state in ("created", "updated"):
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    f"car state is {self.creation_state}, but queue_pull_request_number is None"
                )
            tmp_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
            return [
                context.QueuePullRequest(
                    await self.train.repository.get_pull_request_context(
                        ep.user_pull_request_number
                    ),
                    tmp_ctxt,
                )
                for ep in self.still_queued_embarked_pulls
            ]
        elif self.creation_state == "failed":
            # Will be splitted or dropped soon
            return [
                (
                    await self.train.repository.get_pull_request_context(
                        ep.user_pull_request_number
                    )
                ).pull_request
                for ep in self.still_queued_embarked_pulls
            ]
        else:
            raise RuntimeError(f"Invalid state: {self.creation_state}")

    async def get_context_to_evaluate(self) -> typing.Optional[context.Context]:
        if self.creation_state in ("created", "updated"):
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    f"car state is {self.creation_state}, but queue_pull_request_number is None"
                )
            return await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
        else:
            return None

    def get_queue_name(self) -> "rules.QueueName":
        return self.initial_embarked_pulls[0].config["name"]

    async def is_behind(self) -> bool:
        ctxt = await self.train.repository.get_pull_request_context(
            self.still_queued_embarked_pulls[0].user_pull_request_number
        )
        return await ctxt.is_behind

    @tracer.wrap("TrainCar.start_inplace_checks", span_type="worker")
    async def start_checking_inplace(self, queue_rule: "rules.QueueRule") -> None:
        if len(self.still_queued_embarked_pulls) != 1:
            raise RuntimeError("multiple embarked_pulls but state==updated")

        embarked_pull = self.still_queued_embarked_pulls[0]

        ctxt = await self.train.repository.get_pull_request_context(
            embarked_pull.user_pull_request_number
        )

        if await ctxt.is_behind:
            try:
                # TODO(sileht): fallback to "merge" and None until all configs has
                # the new fields
                await branch_updater.update(
                    self.still_queued_embarked_pulls[0].config.get(
                        "update_method", "merge"
                    ),
                    ctxt,
                    subscription.Features.MERGE_BOT_ACCOUNT,
                    self.still_queued_embarked_pulls[0].config.get(
                        "update_bot_account"
                    ),
                )
            except branch_updater.BranchUpdateFailure as exc:
                await self._set_creation_failure(
                    f"{exc.title}\n\n{exc.message}", operation="update"
                )
                raise TrainCarPullRequestCreationFailure(self) from exc

            # NOTE(sileht): We must update head_sha of the pull request otherwise
            # next temporary pull request may be created on a vanished reference.
            await ctxt.update()

        else:
            # Already done, just refresh it to merge it
            await utils.send_pull_refresh(
                self.train.repository.installation.redis.stream,
                ctxt.pull["base"]["repo"],
                pull_request_number=ctxt.pull["number"],
                action="internal",
                source="updated pull need to be merge",
            )

        await self._set_initial_state(
            "updated",
            queue_rule,
            embarked_pull.user_pull_request_number,
        )

    @staticmethod
    def _get_pulls_branch_ref(embarked_pulls: typing.List[EmbarkedPull]) -> str:
        return "-".join([str(ep.user_pull_request_number) for ep in embarked_pulls])

    @tracer.wrap("TrainCar._create_draft_pull_request", span_type="worker")
    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(
            DraftPullRequestCreationTemporaryFailure
        ),
        stop=tenacity.stop_after_attempt(2),
    )
    async def _create_draft_pull_request(
        self, branch_name: str, github_user: typing.Optional[user_tokens.UserTokensUser]
    ) -> github_types.GitHubPullRequest:

        try:
            title = f"merge-queue: embarking {self._get_embarked_refs()} together"
            body = await self.generate_merge_queue_summary(for_queue_pull_request=True)
            return typing.cast(
                github_types.GitHubPullRequest,
                (
                    await self.train.repository.installation.client.post(
                        f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/pulls",
                        json={
                            "title": title,
                            "body": body,
                            "base": self.train.ref,
                            "head": branch_name,
                            "draft": True,
                        },
                        oauth_token=github_user["oauth_access_token"]
                        if github_user
                        else None,
                    )
                ).json(),
            )
        except http.HTTPClientSideError as e:

            if "A pull request already exists for" in e.message:
                # NOTE(sileht): filter pull request on head is dangerous.
                # head must be organization:ref-name, if the left or the right side of the : is empty
                # all pull requests are returned. So it's better to double checks
                if not (self.train.repository.installation.owner_login and branch_name):
                    raise RuntimeError("Invalid merge-queue head branch")

                head = f"{self.train.repository.installation.owner_login}:{branch_name}"
                closed_pulls = set()
                async for pull in typing.cast(
                    typing.AsyncIterator[github_types.GitHubPullRequest],
                    self.train.repository.installation.client.items(
                        f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/pulls",
                        params={"head": head},
                        resource_name="pulls",
                        page_limit=20,
                    ),
                ):
                    closed_pulls.add(pull["number"])
                    await self.train._close_pull_request(pull["number"])

                self.train.log.info(
                    "fail to create a merge-queue pull request, because the pull request already exists.",
                    head=branch_name,
                    title=title,
                    github_user=github_user["login"] if github_user else None,
                    parent_pull_request_numbers=self.parent_pull_request_numbers,
                    still_queued_embarked_pull_numbers=[
                        ep.user_pull_request_number
                        for ep in self.still_queued_embarked_pulls
                    ],
                    exc_info=True,
                    closed_pulls=closed_pulls,
                )
                raise DraftPullRequestCreationTemporaryFailure(e.message)

            if "Draft pull requests are not supported" not in e.message:
                self.train.log.error(
                    "fail to create a merge-queue pull request",
                    head=branch_name,
                    title=title,
                    github_user=github_user["login"] if github_user else None,
                    parent_pull_request_numbers=self.parent_pull_request_numbers,
                    still_queued_embarked_pull_numbers=[
                        ep.user_pull_request_number
                        for ep in self.still_queued_embarked_pulls
                    ],
                    exc_info=True,
                )

            await self._set_creation_failure(e.message)
            raise TrainCarPullRequestCreationFailure(self) from e

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(tenacity.TryAgain),
        stop=tenacity.stop_after_attempt(2),
    )
    async def _prepare_empty_draft_pr_branch(
        self, branch_name: str, github_user: typing.Optional[user_tokens.UserTokensUser]
    ) -> None:
        try:
            await self.train.repository.installation.client.post(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/git/refs",
                oauth_token=github_user["oauth_access_token"] if github_user else None,
                json={
                    "ref": f"refs/heads/{branch_name}",
                    "sha": self.initial_current_base_sha,
                },
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422 and "Reference already exists" in exc.message:
                try:
                    await self._delete_branch()
                except http.HTTPClientSideError as exc_patch:
                    await self._set_creation_failure(exc_patch.message)
                    raise TrainCarPullRequestCreationFailure(self) from exc_patch

                raise tenacity.TryAgain
            else:
                await self._set_creation_failure(exc.message)
                raise TrainCarPullRequestCreationFailure(self) from exc

    @tracer.wrap("TrainCar.start_checking_with_draft", span_type="worker")
    async def start_checking_with_draft(
        self,
        queue_rule: "rules.QueueRule",
    ) -> None:

        self.head_branch = self._get_pulls_branch_ref(self.initial_embarked_pulls)

        branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.head_branch}"
        )

        bot_account = queue_rule.config["draft_bot_account"]
        github_user: typing.Optional[user_tokens.UserTokensUser] = None
        if bot_account:
            tokens = await self.train.repository.installation.get_user_tokens()
            github_user = tokens.get_token_for(bot_account)
            if not github_user:
                await self._set_creation_failure(
                    f"Unable to create draft pull request: user `{bot_account}` is unknown. "
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )
                raise TrainCarPullRequestCreationFailure(self)

        await self._prepare_empty_draft_pr_branch(branch_name, github_user)

        for pull_number in self.parent_pull_request_numbers + [
            ep.user_pull_request_number for ep in self.still_queued_embarked_pulls
        ]:
            try:
                await self.train.repository.installation.client.post(
                    f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/merges",
                    oauth_token=github_user["oauth_access_token"]
                    if github_user
                    else None,
                    json={
                        "base": branch_name,
                        "head": f"refs/pull/{pull_number}/head",
                        "commit_message": f"Merge of #{pull_number}",
                    },
                )
            except http.HTTPClientSideError as e:
                if (
                    e.status_code == 403
                    and "Resource not accessible by integration" in e.message
                ):
                    self.train.log.info(
                        "fail to create the queue pull request due to GitHub App restriction",
                        embarked_pulls=[
                            ep.user_pull_request_number
                            for ep in self.still_queued_embarked_pulls
                        ],
                        error_message=e.message,
                    )
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationPostponed(self) from e
                elif "Merge conflict" in e.message:
                    pull_requests_ahead = self.parent_pull_request_numbers[:]
                    for ep in self.still_queued_embarked_pulls:
                        if ep.user_pull_request_number == pull_number:
                            break
                        pull_requests_ahead.append(ep.user_pull_request_number)
                    message = "The pull request conflict with at least one of pull request ahead in queue: "
                    message += ", ".join([f"#{p}" for p in pull_requests_ahead])
                    await self._set_creation_failure(
                        message, pull_requests=[pull_number]
                    )
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationFailure(self) from e
                else:
                    await self._set_creation_failure(
                        e.message, pull_requests=[pull_number]
                    )
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationFailure(self) from e

        try:
            tmp_pull = await self._create_draft_pull_request(branch_name, github_user)
        except DraftPullRequestCreationTemporaryFailure as e:
            await self._delete_branch()
            raise TrainCarPullRequestCreationPostponed(self) from e
        await self._set_initial_state("created", queue_rule, tmp_pull["number"])

    async def _set_initial_state(
        self,
        state: typing.Literal["created", "updated"],
        queue_rule: "rules.QueueRule",
        pull_request_number: github_types.GitHubPullRequestNumber,
    ) -> None:
        self.creation_state = state
        self.queue_pull_request_number = pull_request_number

        queue_pull_requests = await self.get_pull_requests_to_evaluate()
        evaluated_queue_rule = await queue_rule.get_evaluated_queue_rule(
            self.train.repository,
            self.train.ref,
            queue_pull_requests,
            self.train.repository.log,
            False,
        )
        await self.update_state(check_api.Conclusion.PENDING, evaluated_queue_rule)
        await self.update_summaries(check_api.Conclusion.PENDING, force_refresh=True)

        for ep in self.still_queued_embarked_pulls:
            position, _ = self.train.find_embarked_pull(ep.user_pull_request_number)
            if position is None:
                raise RuntimeError("TrainCar with embarked_pull not in train...")
            await signals.send(
                self.train.repository,
                ep.user_pull_request_number,
                "action.queue.checks_start",
                signals.EventQueueChecksStartMetadata(
                    {
                        "queue_name": ep.config["name"],
                        "branch": self.train.ref,
                        "position": position,
                        "queued_at": ep.queued_at,
                        "speculative_check_pull_request": {
                            "number": self.queue_pull_request_number,
                            "in_place": self.creation_state == "updated",
                            "checks_conclusion": self.checks_conclusion.value
                            or "pending",
                            "checks_timed_out": self.has_timed_out,
                            "checks_ended_at": self.checks_ended_timestamp,
                        },
                    }
                ),
                "merge-queue internal",
            )

    async def generate_merge_queue_summary(
        self,
        *,
        for_queue_pull_request: bool = False,
        show_queue: bool = True,
        headline: typing.Optional[str] = None,
    ) -> str:
        description = ""
        if headline:
            description += f"**{headline}**\n\n"

        description += (
            f"{self._get_embarked_refs(markdown=True)} are embarked together for merge."
        )

        if for_queue_pull_request:
            description += f"""

This pull request has been created by Mergify to speculatively check the mergeability of {self._get_user_refs()}.
You don't need to do anything. Mergify will close this pull request automatically when it is complete.
"""

        description += await self.train.generate_merge_queue_summary_footer(
            queue_rule_report=QueueRuleReport(
                self.still_queued_embarked_pulls[0].config["name"],
                self.last_evaluated_conditions or "",
            ),
            show_queue=show_queue,
        )
        return description.strip()

    async def delete_pull(
        self,
        reason: typing.Optional[str],
        not_reembarked_pull_request: typing.Optional[
            github_types.GitHubPullRequestNumber
        ] = None,
    ) -> None:
        if not self.queue_pull_request_number or self.head_branch is None:
            return

        if self.creation_state == "created" and reason is not None:
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is created, but queue_pull_request_number is None"
                )

            remaning_embarked_pulls = [
                ep
                for ep in self.initial_embarked_pulls
                if not_reembarked_pull_request is None
                or ep.user_pull_request_number != not_reembarked_pull_request
            ]

            tmp_pull_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
            summary = await tmp_pull_ctxt.get_engine_check_run(constants.SUMMARY_NAME)
            if (
                summary is None
                or summary["conclusion"] == check_api.Conclusion.PENDING.value
            ):
                reason = f"✨ {reason}."
                if remaning_embarked_pulls:
                    reason += f" The pull request {self._get_user_refs(embarked_pulls=remaning_embarked_pulls)} has been re-embarked."
                reason += " ✨"

                body = await self.generate_merge_queue_summary(
                    for_queue_pull_request=True,
                    headline=reason,
                    show_queue=False,
                )

                if tmp_pull_ctxt.body != body:
                    await tmp_pull_ctxt.client.patch(
                        f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                        json={"body": body},
                    )

                await tmp_pull_ctxt.set_summary_check(
                    check_api.Result(
                        check_api.Conclusion.CANCELLED,
                        title=f"The pull request {self._get_user_refs(create_link=False)} has been re-embarked for merge",
                        summary=reason,
                    )
                )
                tmp_pull_ctxt.log.info("train car deleted", reason=reason)

        if reason is None:
            if self.has_timed_out:
                aborted = True
                abort_reason = "Checks have timed out"
            else:
                aborted = self.checks_conclusion is not check_api.Conclusion.SUCCESS
                if aborted:
                    abort_reason = "Checks did not succeed"
                else:
                    abort_reason = ""
        else:
            aborted = True
            abort_reason = reason

        for ep in self.initial_embarked_pulls:
            position, _ = self.train.find_embarked_pull(ep.user_pull_request_number)
            await signals.send(
                self.train.repository,
                ep.user_pull_request_number,
                "action.queue.checks_end",
                signals.EventQueueChecksEndMetadata(
                    {
                        "aborted": aborted,
                        "abort_reason": abort_reason,
                        "queue_name": ep.config["name"],
                        "branch": self.train.ref,
                        "position": position,
                        "queued_at": ep.queued_at,
                        "speculative_check_pull_request": {
                            "number": self.queue_pull_request_number,
                            "in_place": self.creation_state == "updated",
                            "checks_conclusion": self.checks_conclusion.value
                            or "pending",
                            "checks_timed_out": self.has_timed_out,
                            "checks_ended_at": self.checks_ended_timestamp,
                        },
                    }
                ),
                "merge-queue internal",
            )
        await self._delete_branch()

    async def _delete_branch(self) -> None:
        escaped_branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/"
            f"{parse.quote(self.train.ref, safe='')}/"
            f"{self.head_branch}"
        )
        if self.queue_pull_request_number is not None:
            await self.train._close_pull_request(self.queue_pull_request_number)
        await self.train._delete_branch(escaped_branch_name)

    async def _set_creation_failure(
        self,
        details: str,
        *,
        operation: typing.Literal["created", "update"] = "created",
        pull_requests: typing.Optional[
            typing.List[github_types.GitHubPullRequestNumber]
        ] = None,
    ) -> None:
        self.creation_state = "failed"

        title = "This pull request cannot be embarked for merge"

        if self.queue_pull_request_number is None:
            summary = f"The merge-queue pull request can't be {operation}"
        else:
            summary = f"The merge-queue pull request (#{self.queue_pull_request_number}) can't be prepared"

        summary += f"\nDetails: `{details}`"

        # Update the original Pull Requests
        for embarked_pull in self.still_queued_embarked_pulls:
            if (
                pull_requests is not None
                and embarked_pull.user_pull_request_number not in pull_requests
            ):
                continue

            original_ctxt = await self.train.repository.get_pull_request_context(
                embarked_pull.user_pull_request_number
            )
            original_ctxt.log.info(
                "pull request cannot be embarked for merge",
                conclusion=check_api.Conclusion.ACTION_REQUIRED,
                title=title,
                summary=summary,
                details=details,
                exc_info=True,
            )
            await check_api.set_check_run(
                original_ctxt,
                constants.MERGE_QUEUE_SUMMARY_NAME,
                check_api.Result(
                    check_api.Conclusion.ACTION_REQUIRED,
                    title=title,
                    summary=summary,
                ),
            )

            await utils.send_pull_refresh(
                self.train.repository.installation.redis.stream,
                original_ctxt.pull["base"]["repo"],
                pull_request_number=original_ctxt.pull["number"],
                action="internal",
                source="draft pull creation error",
            )

    def checks_have_timed_out(
        self, checks_duration: datetime.datetime, timeout: datetime.timedelta
    ) -> bool:
        return (checks_duration - self.creation_date) > timeout

    async def update_state(
        self,
        checks_conclusion: "checks_status.ChecksCombinedStatus",
        evaluated_queue_rule: "rules.EvaluatedQueueRule",
    ) -> None:
        self.checks_conclusion = checks_conclusion
        self.last_evaluated_conditions = evaluated_queue_rule.conditions.get_summary()
        self.last_checks = []
        self.has_timed_out = False
        self.ci_has_passed = False

        rule = await self.train.get_queue_rule(
            self.initial_embarked_pulls[0].config["name"]
        )
        timeout = rule.config["checks_timeout"]

        if (
            self.checks_ended_timestamp is None
            and self.checks_conclusion != check_api.Conclusion.PENDING
        ):
            self.checks_ended_timestamp = date.utcnow()

        if self.checks_conclusion == check_api.Conclusion.SUCCESS:
            self.ci_has_passed = True
        else:
            conditions_with_only_checks = evaluated_queue_rule.conditions.copy()
            for condition_with_only_checks in conditions_with_only_checks.walk():
                attr = condition_with_only_checks.get_attribute_name()
                if not (attr.startswith("check-") or attr.startswith("status-")):
                    condition_with_only_checks.update("number>0")

            queue_pull_requests = await self.get_pull_requests_to_evaluate()
            self.ci_has_passed = await conditions_with_only_checks(queue_pull_requests)

        if (
            timeout is not None
            and self.checks_conclusion != check_api.Conclusion.FAILURE
            and not self.ci_has_passed
        ):
            self.has_timed_out = self.checks_have_timed_out(date.utcnow(), timeout)

            if self.queue_pull_request_number is not None and not self.has_timed_out:
                # Circular import
                from mergify_engine import delayed_refresh

                await delayed_refresh.plan_refresh_at_least_at(
                    self.train.repository,
                    self.queue_pull_request_number,
                    self.creation_date + timeout,
                )

        if self.creation_state in ("created", "updated"):
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    f"car state is {self.creation_state}, but queue_pull_request_number is None"
                )

            checked_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
        else:
            return

        for check in await checked_ctxt.pull_check_runs:
            # Don't copy Summary/Rule/Queue/... checks
            if check["app_id"] == config.INTEGRATION_ID:
                continue

            output_title = ""
            if check["output"] and check["output"]["title"]:
                output_title = f" — {check['output']['title']}"

            self.last_checks.append(
                QueueCheck(
                    name=f"{check['app_name']}/{check['name']}",
                    description=output_title,
                    avatar_url=check["app_avatar_url"],
                    url=check["html_url"],
                    state=check["conclusion"] or "pending",
                )
            )

        for status in await checked_ctxt.pull_statuses:
            self.last_checks.append(
                QueueCheck(
                    name=status["context"],
                    description=status["description"] or "",
                    avatar_url=status["avatar_url"] or "",
                    url=status["target_url"] or "",
                    state=status["state"] or "pending",
                )
            )

    async def update_summaries(
        self,
        conclusion: "checks_status.ChecksCombinedStatus",
        unexpected_change: typing.Optional[UnexpectedChange] = None,
        force_refresh: bool = False,
    ) -> None:
        refs = self._get_user_refs(create_link=False)
        if conclusion == check_api.Conclusion.SUCCESS:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = f"The pull request {refs} is mergeable"
            else:
                tmp_pull_title = f"The pull requests {refs} are mergeable"
        elif conclusion == check_api.Conclusion.PENDING:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = f"The pull request {refs} is embarked for merge"
            else:
                tmp_pull_title = f"The pull requests {refs} are embarked for merge"
        else:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = (
                    f"The pull request {refs} cannot be merged and has been disembarked"
                )
            else:
                tmp_pull_title = (
                    f"The pull requests {refs} cannot be merged and will be split"
                )

        queue_summary = "\n\nRequired conditions for merge:\n\n"
        queue_summary += self.last_evaluated_conditions or ""
        timeout_summary = ""
        rule = await self.train.get_queue_rule(
            self.initial_embarked_pulls[0].config["name"]
        )
        timeout = rule.config["checks_timeout"]

        if (
            timeout is not None
            and not self.ci_has_passed
            and self.checks_conclusion != check_api.Conclusion.SUCCESS
        ):
            expected_finish = (
                date.pretty_datetime(date.RelativeDatetime(self.creation_date).value)
                if self.creation_date.date() > date.utcnow().date()
                else date.pretty_time(date.RelativeDatetime(self.creation_date).value)
            )
            timeout_summary = (
                f"\n⏲️  The checks have to pass before {expected_finish} ⏲\n️"
                if not self.has_timed_out
                else "\n❌⏲️️  The checks have timed out ⏲❌\n️"
            )

        queue_summary += timeout_summary

        if self.failure_history:
            batch_failure_summary = f"\n\nThe pull request {self._get_user_refs()} is part of a speculative checks batch that previously failed:\n"
            batch_failure_summary += (
                "| Pull request | Parents pull requests | Speculative checks |\n"
            )
            batch_failure_summary += "| ---: | :--- | :--- |\n"
            for failure in self.failure_history:
                if failure.creation_state == "updated":
                    speculative_checks = "[in place]"
                elif failure.creation_state == "created":
                    if failure.queue_pull_request_number is None:
                        raise RuntimeError(
                            "car state is created, but queue_pull_request_number is None"
                        )
                    speculative_checks = build_pr_link(
                        self.train.repository, failure.queue_pull_request_number
                    )
                else:
                    speculative_checks = ""
            batch_failure_summary += f"| {self._get_user_refs()} | {self._get_embarked_refs(include_my_self=False, markdown=True)} | {speculative_checks} |"
        else:
            batch_failure_summary = ""

        original_ctxts = [
            await self.train.repository.get_pull_request_context(
                ep.user_pull_request_number
            )
            for ep in self.still_queued_embarked_pulls
        ]

        if self.creation_state == "created":
            summary = f"Embarking {self._get_embarked_refs(markdown=True)} together"
            summary += queue_summary + "\n" + batch_failure_summary

            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is created, but queue_pull_request_number is None"
                )

            tmp_pull_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )

            headline: typing.Optional[str] = None
            show_queue = True
            if conclusion == check_api.Conclusion.SUCCESS:
                headline = "🎉 This combination of pull requests has been checked successfully 🎉"
                show_queue = False
            elif conclusion == check_api.Conclusion.FAILURE:
                headline = (
                    "🙁 This combination of pull requests has failed checks. "
                    f"{self._get_user_refs()} will be removed from the queue. 🙁"
                )
                show_queue = False
            elif conclusion == check_api.Conclusion.PENDING:
                if unexpected_change is not None:
                    headline = f"✨ Unexpected queue change: {unexpected_change}. The pull request {self._get_user_refs()} will be re-embarked soon. ✨"
                elif self.checks_conclusion == check_api.Conclusion.FAILURE:
                    if self.has_previous_car_status_succeeded():
                        headline = "🕵️  This combination of pull requests has failed checks. Mergify will split this batch to understand which pull request is responsible for the failure. 🕵️"
                    else:
                        headline = "🕵️ This combination of pull requests has failed checks. Mergify is waiting for other pull requests ahead in the queue to understand which one is responsible for the failure. 🕵️"

            body = await self.generate_merge_queue_summary(
                for_queue_pull_request=True,
                headline=headline,
                show_queue=show_queue,
            )

            if tmp_pull_ctxt.body != body:
                await tmp_pull_ctxt.client.patch(
                    f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                    json={"body": body},
                )

            await tmp_pull_ctxt.set_summary_check(
                check_api.Result(
                    conclusion,
                    title=tmp_pull_title,
                    summary=summary,
                )
            )

            checked_pull = self.queue_pull_request_number
        elif self.creation_state == "updated":
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is updated, but queue_pull_request_number is None"
                )
            checked_pull = self.queue_pull_request_number
        else:
            checked_pull = github_types.GitHubPullRequestNumber(0)

        if self.last_checks:
            checks_copy_summary = (
                "\n\nCheck-runs and statuses of the embarked "
                f"pull request #{checked_pull}:\n\n<table>"
            )
            for qcheck in self.last_checks:
                qcheck_icon_url = CHECK_ASSERTS.get(
                    qcheck.state, CHECK_ASSERTS["neutral"]
                )

                checks_copy_summary += (
                    "<tr>"
                    f'<td align="center" width="48" height="48"><img src="{qcheck_icon_url}" width="16" height="16" /></td>'
                    f'<td align="center" width="48" height="48"><img src="{qcheck.avatar_url}&s=40" width="16" height="16" /></td>'
                    f"<td><b>{qcheck.name}</b>{qcheck.description}</td>"
                    f'<td><a href="{qcheck.url}">details</a></td>'
                    "</tr>"
                )
            checks_copy_summary += "</table>\n"
        else:
            checks_copy_summary = ""

        # Update the original Pull Request
        unexpected_change_summary = ""
        if unexpected_change is None:
            if conclusion == check_api.Conclusion.SUCCESS:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} is mergeable"
            elif conclusion == check_api.Conclusion.PENDING:
                original_pull_title = f"The pull request is embarked with {self._get_embarked_refs(include_my_self=False)} for merge"
            else:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} cannot be merged and has been disembarked"
        else:
            original_pull_title = "The pull request is going to be re-embarked soon"
            unexpected_change_summary = (
                f"✨ Unexpected queue change: {unexpected_change}. ✨\n\n"
            )

        if self.has_timed_out:
            conclusion = check_api.Conclusion.FAILURE

        report = check_api.Result(
            conclusion,
            title=original_pull_title,
            summary=unexpected_change_summary
            + queue_summary
            + "\n"
            + checks_copy_summary
            + "\n"
            + batch_failure_summary,
        )
        for original_ctxt in original_ctxts:
            original_ctxt.log.info(
                "pull request train car status update",
                conclusion=conclusion.value,
                report=report,
            )
            await check_api.set_check_run(
                original_ctxt,
                constants.MERGE_QUEUE_SUMMARY_NAME,
                report,
            )

            if force_refresh or (
                self.creation_state == "created"
                and conclusion != check_api.Conclusion.PENDING
            ):
                # NOTE(sileht): refresh it, so the queue action will merge it and delete the
                # tmp_pull_ctxt branch
                await utils.send_pull_refresh(
                    self.train.repository.installation.redis.stream,
                    original_ctxt.pull["base"]["repo"],
                    pull_request_number=original_ctxt.pull["number"],
                    action="internal",
                    source="draft pull request state change",
                )

        if self.creation_state != "created":
            return

        if conclusion in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.FAILURE,
        ]:
            await tmp_pull_ctxt.client.post(
                f"{tmp_pull_ctxt.base_url}/issues/{self.queue_pull_request_number}/comments",
                json={"body": tmp_pull_title},
            )
            await tmp_pull_ctxt.client.patch(
                f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                json={"state": "closed"},
            )

    def _get_previous_car(self) -> typing.Optional["TrainCar"]:
        position = self.train._cars.index(self)
        if position == 0:
            return None
        else:
            return self.train._cars[position - 1]

    def has_previous_car_status_succeeded(self) -> bool:
        position = self.train._cars.index(self)
        if position == 0:
            return True
        return all(
            c.checks_conclusion == check_api.Conclusion.SUCCESS
            for c in self.train._cars[:position]
        )


@dataclasses.dataclass
class Train:
    repository: context.Repository
    ref: github_types.GitHubRefType

    # Stored in redis
    _cars: typing.List[TrainCar] = dataclasses.field(default_factory=list)
    _waiting_pulls: typing.List[EmbarkedPull] = dataclasses.field(default_factory=list)
    _current_base_sha: typing.Optional[github_types.SHAType] = dataclasses.field(
        default=None
    )

    class Serialized(typing.TypedDict):
        cars: typing.List[TrainCar.Serialized]
        waiting_pulls: typing.List[EmbarkedPull.Serialized]
        current_base_sha: typing.Optional[github_types.SHAType]

    @classmethod
    async def from_context(cls, ctxt: context.Context) -> "Train":
        q = cls(ctxt.repository, ctxt.pull["base"]["ref"])
        await q.load()
        return q

    def _get_redis_key(self) -> str:
        return f"merge-trains~{self.repository.installation.owner_id}"

    def _get_redis_hash_key(self) -> str:
        return f"{self.repository.repo['id']}~{self.ref}"

    @classmethod
    @tracer.wrap("Train.refresh_trains", span_type="worker")
    async def refresh_trains(
        cls,
        installation: context.Installation,
    ) -> None:
        trains_key = f"merge-trains~{installation.owner_id}"
        for key in await installation.redis.cache.hkeys(trains_key):
            repo_id_str, ref_str = key.split("~")
            ref = github_types.GitHubRefType(ref_str)
            repo_id = github_types.GitHubRepositoryIdType(int(repo_id_str))
            try:
                repository = await installation.get_repository_by_id(repo_id)
            except http.HTTPNotFound:
                LOG.warning(
                    "repository with active merge-queue is unaccessible, deleting merge-queue",
                    gh_owner=installation.owner_login,
                    gh_repo_id=repo_id,
                )
                await installation.redis.cache.hdel(trains_key, key)
                continue
            train = cls(repository, ref)
            await train.load()
            await train.refresh()

    @classmethod
    async def iter_trains(
        cls,
        repository: context.Repository,
        *,
        exclude_ref: typing.Optional[github_types.GitHubRefType] = None,
    ) -> typing.AsyncIterator["Train"]:
        repo_filter: typing.Union[
            github_types.GitHubRepositoryIdType, typing.Literal["*"]
        ] = "*"
        if repository is not None:
            repo_filter = repository.repo["id"]

        async for key, train_raw in repository.installation.redis.cache.hscan_iter(
            f"merge-trains~{repository.installation.owner_id}",
            f"{repo_filter}~*",
            count=10000,
        ):
            repo_id_str, ref_str = key.split("~")
            ref = github_types.GitHubRefType(ref_str)
            if exclude_ref is not None and ref == exclude_ref:
                continue

            train = cls(repository, ref)
            await train.load(train_raw)
            yield train

    async def load(self, train_raw: typing.Optional[str] = None) -> None:
        if train_raw is None:
            train_raw = await self.repository.installation.redis.cache.hget(
                self._get_redis_key(), self._get_redis_hash_key()
            )

        if train_raw:
            train = typing.cast(Train.Serialized, json.loads(train_raw))
            self._waiting_pulls = [
                EmbarkedPull.deserialize(self, wp) for wp in train["waiting_pulls"]
            ]
            self._current_base_sha = train["current_base_sha"]
            self._cars = [TrainCar.deserialize(self, c) for c in train["cars"]]
        else:
            self._cars = []
            self._waiting_pulls = []
            self._current_base_sha = None

    @property
    def log_queue_extras(self) -> typing.Dict[str, typing.Any]:
        return {
            "train_cars": [
                [ep.user_pull_request_number for ep in c.still_queued_embarked_pulls]
                for c in self._cars
            ],
            "train_waiting_pulls": [
                wp.user_pull_request_number for wp in self._waiting_pulls
            ],
        }

    @functools.cached_property
    def log(self) -> daiquiri.KeywordArgumentAdapter:
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.repo["name"],
            gh_branch=self.ref,
            **self.log_queue_extras,
        )

    async def save(self) -> None:
        if self._waiting_pulls or self._cars:
            prepared = self.Serialized(
                waiting_pulls=[ep.serialized() for ep in self._waiting_pulls],
                current_base_sha=self._current_base_sha,
                cars=[c.serialized() for c in self._cars],
            )
            raw = json.dumps(prepared)
            await self.repository.installation.redis.cache.hset(
                self._get_redis_key(), self._get_redis_hash_key(), raw
            )
        else:
            await self.repository.installation.redis.cache.hdel(
                self._get_redis_key(), self._get_redis_hash_key()
            )

    def get_car(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: ctxt.pull["number"]
            in [ep.user_pull_request_number for ep in car.still_queued_embarked_pulls],
        )

    def get_car_by_tmp_pull(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: car.queue_pull_request_number == ctxt.pull["number"],
        )

    async def get_queue_rules(self) -> typing.Optional["rules.QueueRules"]:
        # circular import
        from mergify_engine import rules

        try:
            mergify_config = await self.repository.get_mergify_config()
        except rules.InvalidRules as e:  # pragma: no cover
            self.log.warning(
                "train can't be refreshed, the mergify configuration is invalid",
                summary=str(e),
                annotations=e.get_annotations(e.filename),
            )
            return None

        return mergify_config["queue_rules"]

    async def get_queue_rule(self, queue_name: "rules.QueueName") -> "rules.QueueRule":
        rules = await self.get_queue_rules()

        if not rules or queue_name not in [rule.name for rule in rules]:
            raise RuntimeError(
                f"The rule for queue `{queue_name}` is missing from configuration"
            )

        return rules[queue_name]

    async def refresh(self) -> None:

        queue_rules = await self.get_queue_rules()
        if queue_rules is None:
            return

        # NOTE(sileht): workaround for cleaning unwanted PRs queued by this bug:
        # https://github.com/Mergifyio/mergify-engine/pull/2958
        await self._remove_duplicate_pulls()
        await self._sync_configuration_change(queue_rules)
        await self._split_failed_batches(queue_rules)
        await self._populate_cars(queue_rules)
        await self.save()

    async def _remove_duplicate_pulls(self) -> None:
        known_prs = set()
        i = 0
        for car in self._cars:
            for embarked_pull in car.still_queued_embarked_pulls:
                if embarked_pull.user_pull_request_number in known_prs:
                    await self._slice_cars(
                        i, reason="The pull request has been queued twice"
                    )
                    break
                else:
                    known_prs.add(embarked_pull.user_pull_request_number)
                i += 1

        wp_to_keep = []
        for wp in self._waiting_pulls:
            if wp.user_pull_request_number not in known_prs:
                known_prs.add(wp.user_pull_request_number)
                wp_to_keep.append(wp)
        self._waiting_pulls = wp_to_keep

    async def _sync_configuration_change(self, queue_rules: "rules.QueueRules") -> None:
        for i, (embarked_pull, _) in enumerate(list(self._iter_embarked_pulls())):
            queue_rule = queue_rules.get(embarked_pull.config["name"])
            if queue_rule is None:
                # NOTE(sileht): We just slice the cars list here, so when the
                # car will be recreated if the rule doesn't exists anymore, the
                # failure will be reported properly
                await self._slice_cars(
                    i, reason="The associated queue rule does not exist anymore"
                )

    async def reset(self, unexpected_change: UnexpectedChange) -> None:
        await self._slice_cars(
            0, reason=f"Unexpected queue change: {unexpected_change}."
        )
        await self.save()
        self.log.info("train cars reset")

    async def _slice_cars(
        self,
        new_queue_size: int,
        reason: str,
        drop_pull_request: typing.Optional[github_types.GitHubPullRequestNumber] = None,
    ) -> None:
        sliced = False
        new_cars: typing.List[TrainCar] = []
        new_waiting_pulls: typing.List[EmbarkedPull] = []
        for c in self._cars:
            new_queue_size -= len(c.still_queued_embarked_pulls)
            if new_queue_size >= 0:
                new_cars.append(c)
            else:
                sliced = True
                new_waiting_pulls.extend(c.still_queued_embarked_pulls)
                await c.delete_pull(
                    reason, not_reembarked_pull_request=drop_pull_request
                )

        if sliced:
            self.log.info(
                "queue has been sliced", new_queue_size=new_queue_size, reason=reason
            )

        self._cars = new_cars
        self._waiting_pulls = [
            ep
            for ep in new_waiting_pulls + self._waiting_pulls
            if drop_pull_request is None
            or ep.user_pull_request_number != drop_pull_request
        ]

    def find_embarked_pull(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> typing.Union[
        typing.Tuple[int, EmbarkedPullWithCar],
        typing.Tuple[typing.Literal[None], typing.Literal[None]],
    ]:
        for position, embarked_pull_with_car in enumerate(self._iter_embarked_pulls()):
            if (
                embarked_pull_with_car.embarked_pull.user_pull_request_number
                == pull_number
            ):
                return position, embarked_pull_with_car
        return None, None

    @staticmethod
    def _waiting_pulls_sorter(
        pull: EmbarkedPull,
    ) -> typing.Tuple[int, datetime.datetime]:
        return (
            pull.config["effective_priority"] * -1,
            pull.queued_at,
        )

    @property
    def _waiting_pulls_ordered_by_priority(self) -> typing.List[EmbarkedPull]:
        return sorted(
            self._waiting_pulls,
            key=self._waiting_pulls_sorter,
        )

    def _iter_embarked_pulls(
        self,
    ) -> typing.Iterator[EmbarkedPullWithCar]:
        for car in self._cars:
            for embarked_pull in car.still_queued_embarked_pulls:
                yield EmbarkedPullWithCar(embarked_pull, car)
        for embarked_pull in self._waiting_pulls_ordered_by_priority:
            # NOTE(sileht): NamedTuple doesn't support multiple inheritance
            # the Protocol can't be inherited
            yield EmbarkedPullWithCar(embarked_pull, None)

    async def add_pull(
        self, ctxt: context.Context, config: queue.PullQueueConfig, signal_trigger: str
    ) -> None:

        # NOTE(sileht): first, ensure the pull is not in another branch
        await self.force_remove_pull(
            ctxt, signal_trigger, exclude_ref=ctxt.pull["base"]["ref"]
        )

        new_pull_queue_rule = await self.get_queue_rule(config["name"])
        best_position = -1
        need_to_be_readded = False
        frozen_queues = {
            freeze.name async for freeze in freeze.QueueFreeze.get_all(self.repository)
        }

        for position, (embarked_pull, car) in enumerate(self._iter_embarked_pulls()):
            embarked_pull_queue_rule = await self.get_queue_rule(
                embarked_pull.config["name"]
            )

            car_can_be_interrupted = car is None or (
                (
                    car.checks_conclusion == check_api.Conclusion.PENDING
                    or (
                        embarked_pull.config["name"] != config["name"]
                        and embarked_pull.config["name"] in frozen_queues
                    )
                )
                and new_pull_queue_rule.config["priority"]
                >= embarked_pull_queue_rule.config["priority"]
                and config["name"]
                not in embarked_pull_queue_rule.config[
                    "disallow_checks_interruption_from_queues"
                ]
            )

            if embarked_pull.user_pull_request_number == ctxt.pull["number"]:
                if (
                    config["effective_priority"]
                    != embarked_pull.config["effective_priority"]
                    or config["name"] != embarked_pull.config["name"]
                ) and car_can_be_interrupted:

                    ctxt.log.info(
                        "pull request already in train but misplaced",
                        config=config,
                        **self.log_queue_extras,
                    )
                    need_to_be_readded = True
                    break

                # already in queue at right place, we are good
                ctxt.log.info(
                    "pull request already in train",
                    config=config,
                    **self.log_queue_extras,
                )
                return

            if (
                best_position == -1
                and config["effective_priority"]
                > embarked_pull.config["effective_priority"]
                and car_can_be_interrupted
            ):
                # We found a car with lower priority
                best_position = position

        if need_to_be_readded:
            # FIXME(sileht): this can be optimised by not dropping spec checks,
            # if the position in the queue does not change
            await self._remove_pull(ctxt, signal_trigger)
            await self.add_pull(ctxt, config, signal_trigger)
            return

        new_embarked_pull = EmbarkedPull(
            self, ctxt.pull["number"], config, date.utcnow()
        )
        self._waiting_pulls.append(new_embarked_pull)

        if best_position != -1:
            await self._slice_cars(
                best_position,
                reason=f"Pull request #{ctxt.pull['number']} with higher priority has been queued",
            )

        await self.save()

        final_position, _ = self.find_embarked_pull(ctxt.pull["number"])
        if final_position is None:
            raise RuntimeError(
                "Could not find the pull request we just added in the queue"
            )

        ctxt.log.info(
            "pull request added to train",
            gh_pull=ctxt.pull["number"],
            position=final_position,
            queue_name=config["name"],
            **self.log_queue_extras,
        )
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.queue.enter",
            signals.EventQueueEnterMetadata(
                {
                    "queue_name": new_embarked_pull.config["name"],
                    "branch": self.ref,
                    "position": final_position,
                    "queued_at": new_embarked_pull.queued_at,
                }
            ),
            signal_trigger,
        )
        # Refresh summary of all pull requests
        await self.refresh_pulls(
            source=f"pull {ctxt.pull['number']} added to queue",
        )

    async def remove_pull(self, ctxt: context.Context, signal_trigger: str) -> None:
        # NOTE(sileht): Remove the pull request from all trains, just in case
        # the base branch change in the meantime
        await self.force_remove_pull(
            ctxt, signal_trigger, exclude_ref=ctxt.pull["base"]["ref"]
        )
        await self._remove_pull(ctxt, signal_trigger)

    async def _remove_pull(self, ctxt: context.Context, signal_trigger: str) -> None:
        if (
            ctxt.pull["merged"]
            and ctxt.pull["base"]["ref"] == self.ref
            and self._cars
            and ctxt.pull["number"]
            == self._cars[0].still_queued_embarked_pulls[0].user_pull_request_number
            and await self.is_synced_with_the_base_branch(await self.get_base_sha())
        ):
            # Head of the train was merged and the base_sha haven't changed, we can keep
            # other running cars
            del self._cars[0].still_queued_embarked_pulls[0]
            if len(self._cars[0].still_queued_embarked_pulls) == 0:
                deleted_car = self._cars[0]
                await deleted_car.delete_pull(reason=None)
                self._cars = self._cars[1:]

            if ctxt.pull["merge_commit_sha"] is None:
                raise RuntimeError("merged pull request without merge_commit_sha set")

            self._current_base_sha = ctxt.pull["merge_commit_sha"]

            await self.save()
            ctxt.log.info(
                "removed from head train", position=0, **self.log_queue_extras
            )
            await self.refresh_pulls(
                source=f"merged pull {ctxt.pull['number']} removed from queue",
                additional_pull_request=ctxt.pull["number"],
            )
            return

        position, embarked_pull = self.find_embarked_pull(ctxt.pull["number"])
        if position is None or embarked_pull is None:
            ctxt.log.info("already absent from train", **self.log_queue_extras)
            return

        await self._slice_cars(
            position,
            reason=f"Pull request #{ctxt.pull['number']} which was ahead in the queue has been dequeued",
            drop_pull_request=ctxt.pull["number"],
        )
        await self.save()
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.queue.leave",
            signals.EventQueueLeaveMetadata(
                {
                    "queue_name": embarked_pull.embarked_pull.config["name"],
                    "branch": self.ref,
                    "position": position,
                    "queued_at": embarked_pull.embarked_pull.queued_at,
                }
            ),
            signal_trigger,
        )

        ctxt.log.info("removed from train", position=position, **self.log_queue_extras)
        await self.refresh_pulls(
            source=f"pull {ctxt.pull['number']} removed from queue",
            additional_pull_request=ctxt.pull["number"],
        )

    async def _split_failed_train_car(
        self, queue_rules: "rules.QueueRules", car: TrainCar
    ) -> None:
        current_queue_position = sum(
            len(c.still_queued_embarked_pulls)
            for c in itertools.takewhile(lambda c: c is not car, self._cars)
        ) + len(car.still_queued_embarked_pulls)
        self.log.info("spliting failed car", position=current_queue_position, car=car)

        queue_name = car.get_queue_name()
        try:
            queue_rule = queue_rules[queue_name]
        except KeyError:
            # We just need to wait the pull request has been removed from
            # the queue by the action
            self.log.info(
                "cant split failed batch TrainCar, queue rule does not exist anymore",
                queue_rules=queue_rules,
                queue_name=queue_name,
            )
            return

        # NOTE(sileht): This batch failed, we can drop everything else
        # after has we known now they will not work, and split this one
        # in two
        await self._slice_cars(
            current_queue_position,
            reason="Pull request ahead in queue failed to get merged",
        )

        # We move this car later at the end to not retest it
        del self._cars[-1]

        # NOTE(sileht): if speculative_checks == 1 we split the batch
        # in two parts, but check only the first one
        parts = max(2, queue_rule.config["speculative_checks"])

        parents: typing.List[EmbarkedPull] = []
        for pos, pulls in enumerate(
            utils.split_list(car.still_queued_embarked_pulls[:-1], parts)
        ):
            self._cars.append(
                TrainCar(
                    train=self,
                    initial_embarked_pulls=pulls,
                    still_queued_embarked_pulls=pulls.copy(),
                    parent_pull_request_numbers=car.parent_pull_request_numbers
                    + [ep.user_pull_request_number for ep in parents],
                    initial_current_base_sha=car.initial_current_base_sha,
                    failure_history=car.failure_history + [car],
                )
            )

            parents += pulls
            # NOTE(sileht): if speculative_checks == 1 we must check
            # only the first car, keep the second one as pending.
            # _populate_cars() will create the second one, when the
            # first car has finished and passed
            if queue_rule.config["speculative_checks"] > 1 or pos == 0:
                try:
                    await self._start_checking_car(queue_rule, self._cars[-1])
                except (
                    TrainCarPullRequestCreationPostponed,
                    TrainCarPullRequestCreationFailure,
                ):
                    self.log.info(
                        "failed to create draft pull request",
                        car=car,
                        exc_info=True,
                    )

        # Update the car to pull that was part of the batch into parent, but keep
        # the result as we already test it.
        car.parent_pull_request_numbers = car.parent_pull_request_numbers + [
            ep.user_pull_request_number for ep in parents
        ]
        car.still_queued_embarked_pulls = [car.still_queued_embarked_pulls[-1]]
        car.initial_embarked_pulls = car.still_queued_embarked_pulls.copy()
        self._cars.append(car)

        # Refresh summary of others
        await self.refresh_pulls(source="batch got split")

    async def _split_failed_batches(self, queue_rules: "rules.QueueRules") -> None:
        if (
            len(self._cars) == 1
            and self._cars[0].checks_conclusion == check_api.Conclusion.FAILURE
            and len(self._cars[0].initial_embarked_pulls) == 1
        ):
            # A earlier batch failed and it was the fault of the last PR of the batch
            # we refresh the draft PR, so it will set the final state
            if self._cars[0].queue_pull_request_number is not None:
                await utils.send_pull_refresh(
                    self.repository.installation.redis.stream,
                    self.repository.repo,
                    pull_request_number=self._cars[0].queue_pull_request_number,
                    action="internal",
                    source="batch failed due to last pull",
                )
            return

        # NOTE(sileht): Looks for batch failure and split if needed
        first_failed_batch_train_car = first.first(
            car
            for car in self._cars
            if (
                car.checks_conclusion == check_api.Conclusion.FAILURE
                and car.has_previous_car_status_succeeded()
                and len(car.initial_embarked_pulls) > 1
            )
        )
        if first_failed_batch_train_car is not None:
            await self._split_failed_train_car(
                queue_rules, first_failed_batch_train_car
            )

        # NOTE(sileht): speculative_checks=1 may create car without the
        # attached draft pull request if this car become the first, it means
        # the previous car has been merged and we can start testing it
        if (
            self._cars
            and len(self._cars[0].failure_history) > 0
            and self._cars[0].creation_state == "pending"
        ):
            queue_name = self._cars[0].get_queue_name()
            try:
                queue_rule = queue_rules[queue_name]
            except KeyError:
                # We just need to wait the pull request has been removed from
                # the queue by the action
                self.log.info(
                    "can't start testing second half of a failed batch TrainCar, queue rule does not exist anymore",
                    queue_rules=queue_rules,
                    queue_name=queue_name,
                )
                return

            try:
                await self._start_checking_car(queue_rule, self._cars[0])
            except TrainCarPullRequestCreationPostponed:
                return
            except TrainCarPullRequestCreationFailure:
                # NOTE(sileht): We posted failure merge-queue check-run on
                # car.user_pull_request_number and refreshed it, so it will be removed
                # from the train soon. We don't need to create remaining cars now.
                # When this car will be removed the remaining one will be created
                return

    async def _populate_cars(self, queue_rules: "rules.QueueRules") -> None:
        if self._cars and (
            self._cars[-1].creation_state == "failed"
            or self._cars[-1].checks_conclusion == check_api.Conclusion.FAILURE
        ):
            # We are searching the responsible of a failure don't touch anything
            return

        try:
            head = next(self._iter_embarked_pulls()).embarked_pull
        except StopIteration:
            return

        if self._current_base_sha is None or not self._cars:
            self._current_base_sha = await self.get_base_sha()

        try:
            queue_rule = queue_rules[head.config["name"]]
        except KeyError:
            # We just need to wait the pull request has been removed from
            # the queue by the action
            self.log.info(
                "cant populate cars, queue rule does not exist",
                queue_rules=queue_rules,
                queue_name=head.config["name"],
            )
            car = TrainCar(self, [head], [head], [], self._current_base_sha)
            await car._set_creation_failure(
                f"queue named `{head.config['name']}` does not exist anymore"
            )
            return

        speculative_checks = queue_rule.config["speculative_checks"]
        missing_cars = speculative_checks - len(self._cars)

        if missing_cars < 0:
            # Too many cars
            new_queue_size = sum(
                [
                    len(car.still_queued_embarked_pulls)
                    for car in self._cars[:speculative_checks]
                ]
            )
            await self._slice_cars(
                new_queue_size,
                reason="The number of speculative checks has been reduced",
            )

        elif missing_cars > 0 and self._waiting_pulls:
            # Not enough cars
            for _ in range(missing_cars):
                pulls_to_check, remaining_pulls = self._get_next_batch(
                    self._waiting_pulls_ordered_by_priority,
                    head.config["name"],
                    queue_rule.config["batch_size"],
                )

                if not pulls_to_check:
                    return

                enough_to_batch = len(pulls_to_check) == queue_rule.config["batch_size"]
                wait_enough_time_to_batch = (
                    date.utcnow() - pulls_to_check[0].queued_at
                    >= queue_rule.config["batch_max_wait_time"]
                )
                if not enough_to_batch and not wait_enough_time_to_batch:
                    # Circular import
                    from mergify_engine import delayed_refresh

                    await delayed_refresh.plan_refresh_at_least_at(
                        self.repository,
                        pulls_to_check[0].user_pull_request_number,
                        pulls_to_check[0].queued_at
                        + queue_rule.config["batch_max_wait_time"],
                    )

                    return

                self._waiting_pulls = remaining_pulls

                # NOTE(sileht): still_queued_embarked_pulls is always in sync with self._current_base_sha.
                # A TrainCar can be partially deleted and the next car may looks wierd as some parent PRs
                # may look missing but because the current_base_sha as moved too, this is safe.
                parent_pull_request_numbers = [
                    ep.user_pull_request_number
                    for ep in itertools.chain.from_iterable(
                        [car.still_queued_embarked_pulls for car in self._cars]
                    )
                ]

                car = TrainCar(
                    self,
                    pulls_to_check,
                    pulls_to_check.copy(),
                    parent_pull_request_numbers,
                    self._current_base_sha,
                )

                self._cars.append(car)

                try:
                    await self._start_checking_car(queue_rule, car)
                except TrainCarPullRequestCreationPostponed:
                    return
                except TrainCarPullRequestCreationFailure:
                    # NOTE(sileht): We posted failure merge-queue check-run on
                    # car.user_pull_request_number and refreshed it, so it will be removed
                    # from the train soon. We don't need to create remaining cars now.
                    # When this car will be removed the remaining one will be created
                    return

    async def _start_checking_car(
        self,
        queue_rule: "rules.QueueRule",
        car: TrainCar,
    ) -> None:
        can_be_updated = (
            self._cars[0] == car
            and len(car.still_queued_embarked_pulls) == 1
            and len(car.parent_pull_request_numbers) == 0
        )
        if can_be_updated and queue_rule.config["allow_inplace_checks"]:
            # smart mode
            if (
                queue_rule.config["speculative_checks"] == 1
                and queue_rule.config["batch_size"] == 1
            ):
                do_inplace_checks = True
            else:
                do_inplace_checks = not await car.is_behind()
        else:
            do_inplace_checks = False

        try:
            # get_next_batch() ensure all embarked_pulls has same config
            if do_inplace_checks:
                # No need to create a pull request
                await car.start_checking_inplace(queue_rule)
            else:
                await car.start_checking_with_draft(queue_rule)

        except TrainCarPullRequestCreationPostponed:
            # NOTE(sileht): We can't create the tmp pull request, we will
            # retry later. In worse case, that will be retried until the pull
            # request become the first one in queue
            del self._cars[-1]
            self._waiting_pulls.extend(car.still_queued_embarked_pulls)
            raise

    async def get_base_sha(self) -> github_types.SHAType:
        escaped_branch_name = parse.quote(self.ref, safe="")
        return typing.cast(
            github_types.GitHubBranch,
            await self.repository.installation.client.item(
                f"repos/{self.repository.installation.owner_login}/{self.repository.repo['name']}/branches/{escaped_branch_name}"
            ),
        )["commit"]["sha"]

    async def is_synced_with_the_base_branch(
        self, base_sha: github_types.SHAType
    ) -> bool:
        if not self._cars:
            return True

        if base_sha == self._current_base_sha:
            return True

        if not self._cars:
            # NOTE(sileht): the PR that call this method will be deleted soon
            return False

        # Base branch just moved but the last merged PR is the one we have on top on our
        # train, we just not yet received the event that have called Train.remove_pull()
        # NOTE(sileht): I wonder if it's robust enough, these cases should be enough to
        # catch everything I have in mind
        # * We run it when we remove the top car
        # * We run it when a tmp PR is refreshed
        # * We run it on each push events
        pull: github_types.GitHubPullRequest = await self.repository.installation.client.item(
            f"{self.repository.base_url}/pulls/{self._cars[0].still_queued_embarked_pulls[0].user_pull_request_number}"
        )
        return pull["merged"] and pull["merge_commit_sha"] == base_sha

    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> queue.PullQueueConfig:
        _, item = self.find_embarked_pull(pull_number)
        if item is not None:
            return item.embarked_pull.config

        raise RuntimeError("get_config on unknown pull request")

    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [
            item.embarked_pull.user_pull_request_number
            for item in self._iter_embarked_pulls()
        ]

    async def is_first_pull(self, ctxt: context.Context) -> bool:
        item = first.first(self._iter_embarked_pulls())
        return (
            item is not None
            and item.embarked_pull.user_pull_request_number == ctxt.pull["number"]
        )

    @staticmethod
    def _get_next_batch(
        pulls: typing.List[EmbarkedPull], queue_name: str, batch_size: int = 1
    ) -> typing.Tuple[typing.List[EmbarkedPull], typing.List[EmbarkedPull]]:
        if not pulls:
            return [], []

        for _i, pull in enumerate(pulls[:batch_size]):
            if pull.config["name"] != queue_name:
                # The queue change, wait first queue to be empty before processing
                # the next queue
                break
        else:
            _i += 1
        return pulls[:_i], pulls[_i:]

    @classmethod
    async def force_remove_pull(
        cls,
        ctxt: context.Context,
        signal_trigger: str,
        *,
        exclude_ref: typing.Optional[github_types.GitHubRefType] = None,
    ) -> None:
        async for train in cls.iter_trains(
            ctxt.repository,
            exclude_ref=exclude_ref,
        ):
            await train._remove_pull(ctxt, signal_trigger)

    async def generate_merge_queue_summary_footer(
        self,
        queue_rule_report: QueueRuleReport,
        *,
        show_queue: bool = True,
    ) -> str:

        description = f"\n\n**Required conditions of queue** `{queue_rule_report.name}` **for merge:**\n\n"
        description += queue_rule_report.summary

        if show_queue:
            table = [
                "| | Pull request | Queue/Priority | Speculative checks | Queued",
                "| ---: | :--- | :--- | :--- | :--- |",
            ]
            for i, (embarked_pull, car) in enumerate(self._iter_embarked_pulls()):
                ctxt = await self.repository.get_pull_request_context(
                    embarked_pull.user_pull_request_number
                )
                # NOTE(sileht): we use this wierd url format to not trigger the GitHub pull request cross references
                # [#1234](/Mergifyio/mergify-engine/pull/1234]
                try:
                    fancy_priority = queue.PriorityAliases(
                        embarked_pull.config["priority"]
                    ).name
                except ValueError:
                    fancy_priority = str(embarked_pull.config["priority"])

                speculative_checks = ""
                if car is not None:
                    if car.creation_state == "updated":
                        speculative_checks = build_pr_link(
                            self.repository,
                            embarked_pull.user_pull_request_number,
                            "in place",
                        )
                    elif car.creation_state == "created":
                        if car.queue_pull_request_number is None:
                            raise RuntimeError(
                                "car state is created, but queue_pull_request_number is None"
                            )

                        speculative_checks = build_pr_link(
                            self.repository, car.queue_pull_request_number
                        )

                queued_at = date.pretty_datetime(embarked_pull.queued_at)
                table.append(
                    f"| {i + 1} "
                    f"| {ctxt.pull['title']} ({build_pr_link(self.repository, embarked_pull.user_pull_request_number)}) "
                    f"| {embarked_pull.config['name']}/{fancy_priority} "
                    f"| {speculative_checks} "
                    f"| {queued_at}"
                    "|"
                )

            description += (
                "\n**The following pull requests are queued:**\n"
                + "\n".join(table)
                + "\n"
            )

        description += "\n---\n\n"
        description += constants.MERGIFY_MERGE_QUEUE_PULL_REQUEST_DOC
        return description

    async def get_pull_summary(
        self, ctxt: context.Context, queue_rule: "rules.QueueRule"
    ) -> str:
        # NOTE(sileht): beware before using this method, car.update_state() must have been called earlier
        # to have up2date informations
        _, ep = self.find_embarked_pull(ctxt.pull["number"])
        if ep is None:
            return ""
        if ep.car is None:
            description = f"#{ctxt.pull['number']} is queued for merge."
            description += await self.generate_merge_queue_summary_footer(
                queue_rule_report=QueueRuleReport(
                    name=ep.embarked_pull.config["name"],
                    summary=queue_rule.conditions.get_summary(),
                )
            )
            return description.strip()
        else:
            return await ep.car.generate_merge_queue_summary()

    async def _delete_branch(self, escaped_branch_name: str) -> None:
        try:
            await self.repository.installation.client.delete(
                f"/repos/{self.repository.installation.owner_login}/{self.repository.repo['name']}/git/refs/heads/{escaped_branch_name}"
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 404 or (
                exc.status_code == 422 and "Reference does not exist" in exc.message
            ):
                self.log.warning(
                    "fail to delete merge-queue branch",
                    branch=escaped_branch_name,
                    exc_info=True,
                )
            else:
                raise

    async def _close_pull_request(
        self, pull_request_number: github_types.GitHubPullRequestNumber
    ) -> None:
        try:
            await self.repository.installation.client.patch(
                f"/repos/{self.repository.installation.owner_login}/{self.repository.repo['name']}/pulls/{pull_request_number}",
                json={"state": "closed"},
            )
        except http.HTTPNotFound:
            self.log.warning(
                "fail to close merge-queue pull request",
                pull_request_number=pull_request_number,
                exc_info=True,
            )

    async def refresh_pulls(
        self,
        source: str,
        additional_pull_request: typing.Optional[
            github_types.GitHubPullRequestNumber
        ] = None,
    ) -> None:

        pulls = set(await self.get_pulls())
        if additional_pull_request:
            pulls.add(additional_pull_request)

        pipe = await self.repository.installation.redis.stream.pipeline()
        for pull_number in pulls:
            await utils.send_pull_refresh(
                pipe,
                self.repository.repo,
                pull_request_number=pull_number,
                action="internal",
                source=source,
            )
        await pipe.execute()
