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

import abc
import datetime
import importlib.metadata
import typing

import daiquiri

from mergify_engine import github_types


if typing.TYPE_CHECKING:
    from mergify_engine import context


LOG = daiquiri.getLogger(__name__)

EventName = typing.Literal[
    "action.assign",
    "action.backport",
    "action.close",
    "action.comment",
    "action.copy",
    "action.delete_head_branch",
    "action.dismiss_reviews",
    "action.edit",
    "action.label",
    "action.merge",
    "action.post_check",
    "action.queue.enter",
    "action.queue.checks_start",
    "action.queue.checks_end",
    "action.queue.leave",
    "action.queue.merged",
    "action.rebase",
    "action.refresh",
    "action.request_reviewers",
    "action.requeue",
    "action.review",
    "action.squash",
    "action.unqueue",
    "action.update",
]


class EventMetadata(typing.TypedDict):
    pass


class EventReviewMetadata(EventMetadata, total=False):
    type: str
    reviewer: str


class EventCopyMetadata(EventMetadata, total=False):
    to: str


class EventAssignMetadata(EventMetadata, total=False):
    added: typing.List[str]
    removed: typing.List[str]


class EventEditMetadata(EventMetadata, total=False):
    draft: bool


class EventLabelMetadata(EventMetadata, total=False):
    added: typing.List[str]
    removed: typing.List[str]


class EventPostCheckMetadata(EventMetadata, total=False):
    conclusion: str
    title: str
    summary: str


class EventRequestReviewsMetadata(EventMetadata, total=False):
    reviewers: typing.List[str]
    team_reviewers: typing.List[str]


class EventDismissReviewsMetadata(EventMetadata, total=False):
    users: typing.List[str]


class EventQueueEnterMetadata(EventMetadata, total=False):
    queue_name: str
    branch: str
    position: int
    queued_at: datetime.datetime


class EventQueueLeaveMetadata(EventMetadata, total=False):
    queue_name: str
    branch: str
    position: int
    queued_at: datetime.datetime


# Like GitHubCheckRunConclusion but with "pending" instead of None
ChecksConclusion = typing.Literal[
    "success",
    "failure",
    "error",
    "cancelled",
    "skipped",
    "action_required",
    "timed_out",
    "neutral",
    "stale",
    "pending",
]


class SpeculativeCheckPullRequest(typing.TypedDict, total=False):
    number: int
    in_place: bool
    checks_timed_out: bool
    checks_conclusion: ChecksConclusion
    checks_ended_at: typing.Optional[datetime.datetime]


class EventQueueMergedMetadata(EventMetadata, total=False):
    queue_name: str
    branch: str
    queued_at: datetime.datetime


class EventQueueChecksEndMetadata(EventMetadata, total=False):
    aborted: bool
    abort_reason: typing.Optional[str]
    queue_name: str
    branch: str
    position: typing.Optional[int]
    queued_at: datetime.datetime
    speculative_check_pull_request: SpeculativeCheckPullRequest


class EventQueueChecksStartMetadata(EventMetadata, total=False):
    queue_name: str
    branch: str
    position: int
    queued_at: datetime.datetime
    speculative_check_pull_request: SpeculativeCheckPullRequest


class EventNoMetadata(EventMetadata):
    pass


SignalT = typing.Callable[
    [
        "context.Repository",
        github_types.GitHubPullRequestNumber,
        EventName,
        typing.Optional[EventMetadata],
        str,
    ],
    typing.Coroutine[None, None, None],
]


class SignalBase(abc.ABC):
    @abc.abstractmethod
    async def __call__(
        self,
        repository: "context.Repository",
        pull_request: github_types.GitHubPullRequestNumber,
        event: EventName,
        metadata: EventMetadata,
        trigger: str,
    ) -> None:
        pass


class NoopSignal(SignalBase):
    async def __call__(
        self,
        repository: "context.Repository",
        pull_request: github_types.GitHubPullRequestNumber,
        event: EventName,
        metadata: EventMetadata,
        trigger: str,
    ) -> None:
        pass


global SIGNALS
SIGNALS: typing.Dict[str, SignalT] = {}


def unregister() -> None:
    global SIGNALS
    SIGNALS = {}


def register() -> None:
    global SIGNALS
    for ep in importlib.metadata.entry_points(group="mergify_signals"):
        try:
            # NOTE(sileht): literal import is safe here, we control installed signal packages
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            SIGNALS[ep.name] = ep.load()()
        except ImportError:
            LOG.error("failed to load signal: %s", ep.name, exc_info=True)


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal[
        "action.close",
        "action.comment",
        "action.delete_head_branch",
        "action.merge",
        "action.squash",
        "action.rebase",
        "action.refresh",
        "action.requeue",
        "action.unqueue",
        "action.update",
    ],
    metadata: EventNoMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.review"],
    metadata: EventReviewMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.assign"],
    metadata: EventAssignMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.edit"],
    metadata: EventEditMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.label"],
    metadata: EventLabelMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.post_check"],
    metadata: EventPostCheckMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.dismiss_reviews"],
    metadata: EventDismissReviewsMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.request_reviewers"],
    metadata: EventRequestReviewsMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.backport", "action.copy"],
    metadata: EventCopyMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.queue.enter"],
    metadata: EventQueueEnterMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.queue.leave"],
    metadata: EventQueueLeaveMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.queue.checks_end"],
    metadata: EventQueueChecksEndMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.queue.checks_start"],
    metadata: EventQueueChecksStartMetadata,
    trigger: str,
) -> None:
    ...


@typing.overload
async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: typing.Literal["action.queue.merged"],
    metadata: EventQueueMergedMetadata,
    trigger: str,
) -> None:
    ...


async def send(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
    event: EventName,
    metadata: EventMetadata,
    trigger: str,
) -> None:
    for name, signal in SIGNALS.items():
        try:
            await signal(repository, pull_request, event, metadata, trigger)
        except Exception:
            LOG.error("failed to run signal: %s", name, exc_info=True)
