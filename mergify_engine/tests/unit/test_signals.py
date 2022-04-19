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

from unittest import mock

from mergify_engine import github_types
from mergify_engine import signals
from mergify_engine.tests.unit import conftest


async def test_signals(context_getter: conftest.ContextGetterFixture) -> None:
    signals.setup()
    assert len(signals.SIGNALS) == 3

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    with mock.patch("mergify_engine_signals.noop.Signal.__call__") as signal_method:
        await signals.send(ctxt, "action.update", {"attr": "value"})
        signal_method.assert_called_once_with(ctxt, "action.update", {"attr": "value"})


async def test_datadog():
    signals.setup()
    assert len(signals.SIGNALS) == 3

    ctxt = mock.Mock(
        subscription=mock.Mock(active=True),
        pull={"base": {"user": {"type": "Organization", "id": 123456789}}},
    )

    with mock.patch("datadog.statsd.increment") as increment:
        await signals.send(ctxt, "action.update", {"attr", "value"})
        increment.assert_called_once_with(
            "engine.signals.action.count", tags=["event:update"]
        )
