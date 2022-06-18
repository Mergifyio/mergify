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
from mergify_engine import signals
from mergify_engine.dashboard import subscription


if typing.TYPE_CHECKING:
    from mergify_engine import context

LOG = daiquiri.getLogger(__name__)

EVENTLOGS_LONG_RETENTION = datetime.timedelta(days=7)
EVENTLOGS_SHORT_RETENTION = datetime.timedelta(days=1)


class EventBase(typing.TypedDict):
    timestamp: datetime.datetime


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


class EventPostCheck(EventBaseNoMetadata):
    event: typing.Literal["action.post_check"]


class EventQueueMerged(EventBaseNoMetadata):
    event: typing.Literal["action.queue.merged"]


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
    EventLabel,
    EventMerge,
    EventPostCheck,
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

        key = _get_pull_request_key(
            repository.installation.owner_id, repository.repo["id"], pull_request
        )
        pipe = await redis.pipeline()
        now = date.utcnow()

        await pipe.xadd(
            key,
            fields={
                b"version": DEFAULT_VERSION,
                b"data": msgpack.packb(
                    GenericEvent(
                        {
                            "timestamp": now,
                            "event": event,
                            "metadata": metadata,
                        }
                    ),
                    datetime=True,
                ),
            },
        )
        await pipe.expire(key, int(retention.total_seconds()))
        await pipe.execute()


async def get(
    repository: "context.Repository",
    pull_request: github_types.GitHubPullRequestNumber,
) -> typing.AsyncIterator[Event]:
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

    key = _get_pull_request_key(
        repository.installation.owner_id, repository.repo["id"], pull_request
    )
    older_event = date.utcnow() - retention

    for _, raw in await redis.xrange(key, min=f"{int(older_event.timestamp())}"):
        event = typing.cast(GenericEvent, msgpack.unpackb(raw[b"data"], timestamp=3))
        if event["event"] == "action.assign":
            yield typing.cast(EventAssign, event)

        elif event["event"] == "action.backport":
            yield typing.cast(EventBackport, event)

        elif event["event"] == "action.close":
            yield typing.cast(EventClose, event)

        elif event["event"] == "action.comment":
            yield typing.cast(EventComment, event)

        elif event["event"] == "action.copy":
            yield typing.cast(EventCopy, event)

        elif event["event"] == "action.delete_head_branch":
            yield typing.cast(EventDeleteHeadBranch, event)

        elif event["event"] == "action.dismiss_reviews":
            yield typing.cast(EventDismissReviews, event)

        elif event["event"] == "action.label":
            yield typing.cast(EventLabel, event)

        elif event["event"] == "action.merge":
            yield typing.cast(EventMerge, event)

        elif event["event"] == "action.post_check":
            yield typing.cast(EventPostCheck, event)

        elif event["event"] == "action.queue.merged":
            yield typing.cast(EventQueueMerged, event)

        elif event["event"] == "action.rebase":
            yield typing.cast(EventRebase, event)

        elif event["event"] == "action.refresh":
            yield typing.cast(EventRefresh, event)

        elif event["event"] == "action.request_reviewers":
            yield typing.cast(EventRequestReviewers, event)

        elif event["event"] == "action.requeue":
            yield typing.cast(EventRequeue, event)

        elif event["event"] == "action.review":
            yield typing.cast(EventReview, event)

        elif event["event"] == "action.squash":
            yield typing.cast(EventSquash, event)

        elif event["event"] == "action.unqueue":
            yield typing.cast(EventUnqueue, event)

        elif event["event"] == "action.update":
            yield typing.cast(EventUpdate, event)
        else:
            LOG.error("unsupported event-type, skipping", event=event)
