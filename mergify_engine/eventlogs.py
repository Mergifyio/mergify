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
import itertools
import typing

import daiquiri
import msgpack

from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import pagination
from mergify_engine import signals
from mergify_engine.dashboard import subscription


if typing.TYPE_CHECKING:
    from mergify_engine import context

LOG = daiquiri.getLogger(__name__)

EVENTLOGS_LONG_RETENTION = datetime.timedelta(days=7)
EVENTLOGS_SHORT_RETENTION = datetime.timedelta(days=1)


class EventBase(typing.TypedDict):
    timestamp: datetime.datetime
    trigger: str
    repository: str
    pull_request: int


class EventMetadata(typing.TypedDict):
    pass


class EventBaseNoMetadata(EventBase):
    metadata: signals.EventNoMetadata


class EventAssign(EventBase):
    event: typing.Literal["action.assign"]
    metadata: signals.EventAssignMetadata


class EventBackport(EventBase):
    event: typing.Literal["action.backport"]
    metadata: signals.EventCopyMetadata


class EventClose(EventBaseNoMetadata):
    event: typing.Literal["action.close"]


class EventComment(EventBaseNoMetadata):
    event: typing.Literal["action.comment"]


class EventCopy(EventBase):
    event: typing.Literal["action.copy"]
    metadata: signals.EventCopyMetadata


class EventEdit(EventBase):
    event: typing.Literal["action.edit"]
    metadata: signals.EventEditMetadata


class EventDeleteHeadBranch(EventBaseNoMetadata):
    event: typing.Literal["action.delete_head_branch"]


class EventDismissReviews(EventBase):
    event: typing.Literal["action.dismiss_reviews"]
    metadata: signals.EventDismissReviewsMetadata


class EventLabel(EventBase):
    event: typing.Literal["action.label"]
    metadata: signals.EventLabelMetadata


class EventMerge(EventBaseNoMetadata):
    event: typing.Literal["action.merge"]


class EventPostCheck(EventBase):
    event: typing.Literal["action.post_check"]
    metadata: signals.EventPostCheckMetadata


class EventQueueEnter(EventBase):
    event: typing.Literal["action.queue.enter"]
    metadata: signals.EventQueueEnterMetadata


class EventQueueLeave(EventBase):
    event: typing.Literal["action.queue.leave"]
    metadata: signals.EventQueueLeaveMetadata


class EventQueueChecksStart(EventBase):
    event: typing.Literal["action.queue.checks_start"]
    metadata: signals.EventQueueChecksStartMetadata


class EventQueueChecksEnd(EventBase):
    event: typing.Literal["action.queue.checks_end"]
    metadata: signals.EventQueueChecksEndMetadata


class EventQueueMerged(EventBase):
    event: typing.Literal["action.queue.merged"]
    metadata: signals.EventQueueMergedMetadata


class EventRebase(EventBaseNoMetadata):
    event: typing.Literal["action.rebase"]


class EventRefresh(EventBaseNoMetadata):
    event: typing.Literal["action.refresh"]


class EventRequeue(EventBaseNoMetadata):
    event: typing.Literal["action.requeue"]


class EventRequestReviewers(EventBase):
    event: typing.Literal["action.request_reviewers"]
    metadata: signals.EventRequestReviewsMetadata


class EventReview(EventBase):
    event: typing.Literal["action.review"]
    metadata: signals.EventReviewMetadata


class EventSquash(EventBaseNoMetadata):
    event: typing.Literal["action.squash"]


class EventUnqueue(EventBaseNoMetadata):
    event: typing.Literal["action.unqueue"]


class EventUpdate(EventBaseNoMetadata):
    event: typing.Literal["action.update"]


def _get_repository_key(
    owner_id: github_types.GitHubAccountIdType,
    repo_id: github_types.GitHubRepositoryIdType,
) -> str:
    return f"eventlogs-repository/{owner_id}/{repo_id}"


def _get_pull_request_key(
    owner_id: github_types.GitHubAccountIdType,
    repo_id: github_types.GitHubRepositoryIdType,
    pull_request: github_types.GitHubPullRequestNumber,
) -> str:
    return f"eventlogs/{owner_id}/{repo_id}/{pull_request}"


Event = typing.Union[
    EventAssign,
    EventBackport,
    EventClose,
    EventComment,
    EventCopy,
    EventDeleteHeadBranch,
    EventDismissReviews,
    EventEdit,
    EventLabel,
    EventMerge,
    EventPostCheck,
    EventQueueEnter,
    EventQueueLeave,
    EventQueueChecksStart,
    EventQueueChecksEnd,
    EventQueueMerged,
    EventRebase,
    EventRefresh,
    EventRequestReviewers,
    EventRequeue,
    EventReview,
    EventSquash,
    EventUnqueue,
    EventUpdate,
]

SUPPORTED_EVENT_NAMES = list(
    itertools.chain(
        *[
            evt.__annotations__["event"].__args__
            for evt in Event.__args__  # type: ignore[attr-defined]
        ]
    )
)

DEFAULT_VERSION = "1.0"


class GenericEvent(EventBase):
    event: signals.EventName
    metadata: signals.EventMetadata


class EventLogsSignal(signals.SignalBase):
    async def __call__(
        self,
        repository: "context.Repository",
        pull_request: github_types.GitHubPullRequestNumber,
        event: signals.EventName,
        metadata: signals.EventMetadata,
        trigger: str,
    ) -> None:
        if event not in SUPPORTED_EVENT_NAMES:
            return

        redis = repository.installation.redis.eventlogs
        if redis is None:
            return

        if repository.installation.subscription.has_feature(
            subscription.Features.EVENTLOGS_LONG
        ):
            retention = EVENTLOGS_LONG_RETENTION
        elif repository.installation.subscription.has_feature(
            subscription.Features.EVENTLOGS_SHORT
        ):
            retention = EVENTLOGS_SHORT_RETENTION
        else:
            return

        repo_key = _get_repository_key(
            repository.installation.owner_id, repository.repo["id"]
        )
        pull_key = _get_pull_request_key(
            repository.installation.owner_id, repository.repo["id"], pull_request
        )
        pipe = await redis.pipeline()
        now = date.utcnow()
        fields = {
            b"version": DEFAULT_VERSION,
            b"data": msgpack.packb(
                GenericEvent(
                    {
                        "timestamp": now,
                        "event": event,
                        "repository": repository.repo["full_name"],
                        "pull_request": pull_request,
                        "metadata": metadata,
                        "trigger": trigger,
                    }
                ),
                datetime=True,
            ),
        }
        await pipe.xadd(pull_key, fields=fields)
        await pipe.xadd(repo_key, fields=fields)
        await pipe.expire(pull_key, int(retention.total_seconds()))
        await pipe.expire(repo_key, int(retention.total_seconds()))
        await pipe.execute()


class InvalidCursor(Exception):
    cursor: str


async def get(
    repository: "context.Repository",
    page: pagination.CurrentPage,
    pull_request: typing.Optional[github_types.GitHubPullRequestNumber] = None,
) -> pagination.Page[Event]:
    redis = repository.installation.redis.eventlogs
    if redis is None:
        return pagination.Page([], page)

    if repository.installation.subscription.has_feature(
        subscription.Features.EVENTLOGS_LONG
    ):
        retention = EVENTLOGS_LONG_RETENTION
    elif repository.installation.subscription.has_feature(
        subscription.Features.EVENTLOGS_SHORT
    ):
        retention = EVENTLOGS_SHORT_RETENTION
    else:
        return pagination.Page([], page)

    if pull_request is None:
        key = _get_repository_key(
            repository.installation.owner_id, repository.repo["id"]
        )
    else:
        key = _get_pull_request_key(
            repository.installation.owner_id, repository.repo["id"], pull_request
        )

    older_event = date.utcnow() - retention
    older_event_id = f"{int(older_event.timestamp())}"

    pipe = await redis.pipeline()

    if not page.cursor:
        # first page
        from_ = "+"
        to_ = older_event_id
        look_backward = False
    elif page.cursor == "-":
        # last page
        from_ = "-"
        to_ = "+"
        look_backward = True
    elif page.cursor.startswith("+"):
        # next page
        from_ = f"({page.cursor[1:]}"
        to_ = older_event_id
        look_backward = False
    elif page.cursor.startswith("-"):
        # prev page
        from_ = f"({page.cursor[1:]}"
        to_ = "+"
        look_backward = True
    else:
        raise InvalidCursor(page.cursor)

    await pipe.xlen(key)
    if look_backward:
        await pipe.xrange(key, min=from_, max=to_, count=page.per_page)
    else:
        await pipe.xrevrange(key, max=from_, min=to_, count=page.per_page)

    total, items = await pipe.execute()

    if look_backward:
        items = list(reversed(items))

    if page.cursor == "-":
        # NOTE(sileht): On last page, as we look backward and the query doesn't
        # use event ID, we may have more item that we need. So here, we remove
        # those that shouldn't be there.
        items = items[(total % page.per_page) :]

    if page.cursor and items:
        cursor_prev = f"-{items[0][0].decode()}"
    else:
        cursor_prev = None
    if items:
        cursor_next = f"+{items[-1][0].decode()}"
    else:
        cursor_next = None

    events: typing.List[Event] = []
    for _, raw in items:
        event = typing.cast(GenericEvent, msgpack.unpackb(raw[b"data"], timestamp=3))
        if event["event"] == "action.assign":
            events.append(typing.cast(EventAssign, event))

        elif event["event"] == "action.backport":
            events.append(typing.cast(EventBackport, event))

        elif event["event"] == "action.close":
            events.append(typing.cast(EventClose, event))

        elif event["event"] == "action.comment":
            events.append(typing.cast(EventComment, event))

        elif event["event"] == "action.copy":
            events.append(typing.cast(EventCopy, event))

        elif event["event"] == "action.delete_head_branch":
            events.append(typing.cast(EventDeleteHeadBranch, event))

        elif event["event"] == "action.dismiss_reviews":
            events.append(typing.cast(EventDismissReviews, event))

        elif event["event"] == "action.label":
            events.append(typing.cast(EventLabel, event))

        elif event["event"] == "action.merge":
            events.append(typing.cast(EventMerge, event))

        elif event["event"] == "action.post_check":
            events.append(typing.cast(EventPostCheck, event))

        elif event["event"] == "action.queue.checks_start":
            events.append(typing.cast(EventQueueChecksStart, event))

        elif event["event"] == "action.queue.checks_end":
            events.append(typing.cast(EventQueueChecksEnd, event))

        elif event["event"] == "action.queue.enter":
            events.append(typing.cast(EventQueueEnter, event))

        elif event["event"] == "action.queue.leave":
            events.append(typing.cast(EventQueueLeave, event))

        elif event["event"] == "action.queue.merged":
            events.append(typing.cast(EventQueueMerged, event))

        elif event["event"] == "action.rebase":
            events.append(typing.cast(EventRebase, event))

        elif event["event"] == "action.refresh":
            events.append(typing.cast(EventRefresh, event))

        elif event["event"] == "action.request_reviewers":
            events.append(typing.cast(EventRequestReviewers, event))

        elif event["event"] == "action.requeue":
            events.append(typing.cast(EventRequeue, event))

        elif event["event"] == "action.review":
            events.append(typing.cast(EventReview, event))

        elif event["event"] == "action.squash":
            events.append(typing.cast(EventSquash, event))

        elif event["event"] == "action.unqueue":
            events.append(typing.cast(EventUnqueue, event))

        elif event["event"] == "action.update":
            events.append(typing.cast(EventUpdate, event))

        elif event["event"] == "action.edit":
            events.append(typing.cast(EventEdit, event))

        else:
            LOG.error("unsupported event-type, skipping", event=event)

    return pagination.Page(
        items=events,
        current=page,
        total=total,
        cursor_prev=cursor_prev,
        cursor_next=cursor_next,
    )
