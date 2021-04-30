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
import importlib
import pkgutil
import typing

import daiquiri

from mergify_engine import context


LOG = daiquiri.getLogger(__name__)

EventName = typing.Literal[
    "action.assign.added",
    "action.assign.removed",
    "action.backport",
    "action.copy",
    "action.close",
    "action.comment",
    "action.delete_head_branch",
    "action.dismiss_reviews",
    "action.label",
    "action.merge",
    "action.queue",
    "action.post_check",
    "action.rebase",
    "action.refresh",
    "action.request_reviewers",
    "action.review",
    "action.squash",
    "action.update",
]

SignalMetadata = typing.Dict[str, typing.Union[str, int, float, bool]]
SignalT = typing.Callable[
    [context.Context, EventName, typing.Optional[SignalMetadata]],
    typing.Coroutine[None, None, None],
]


class SignalBase(abc.ABC):
    @abc.abstractmethod
    async def __call__(
        self,
        ctxt: context.Context,
        event: EventName,
        metadata: typing.Optional[SignalMetadata],
    ) -> None:
        pass


global SIGNALS
SIGNALS: typing.Dict[str, SignalT] = {}


def setup() -> None:
    try:
        import mergify_engine_signals
    except ImportError:
        LOG.info("No signals found")
        return

    # Only support new native python >= 3.6 namespace packages
    for mod in pkgutil.iter_modules(
        mergify_engine_signals.__path__, mergify_engine_signals.__name__ + "."
    ):
        try:
            SIGNALS[mod.name] = importlib.import_module(mod.name).Signal()  # type: ignore
        except ImportError:
            LOG.error("failed to load signal: %s", mod.name, exc_info=True)


async def send(
    ctxt: context.Context,
    event: EventName,
    metadata: typing.Optional[SignalMetadata] = None,
) -> None:
    for name, signal in SIGNALS.items():
        try:
            await signal(ctxt, event, metadata)
        except Exception:
            LOG.error("failed to run signal: %s", name, exc_info=True)
