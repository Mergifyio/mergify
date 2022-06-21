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

import pytest

from mergify_engine import github_types
from mergify_engine import signals
from mergify_engine.tests.unit import conftest


async def test_signals(
    context_getter: conftest.ContextGetterFixture, request: pytest.FixtureRequest
) -> None:
    signals.register()
    request.addfinalizer(signals.unregister)
    assert len(signals.SIGNALS) == 4

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    with mock.patch("mergify_engine.signals.NoopSignal.__call__") as signal_method:
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.label",
            signals.EventLabelMetadata({"added": [], "removed": ["bar"]}),
            "Rule: awesome rule",
        )
        signal_method.assert_called_once_with(
            ctxt.repository,
            ctxt.pull["number"],
            "action.label",
            {"added": [], "removed": ["bar"]},
            "Rule: awesome rule",
        )


async def test_datadog(
    context_getter: conftest.ContextGetterFixture, request: pytest.FixtureRequest
) -> None:
    signals.register()
    request.addfinalizer(signals.unregister)
    assert len(signals.SIGNALS) == 4

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))

    with mock.patch("datadog.statsd.increment") as increment:
        await signals.send(
            ctxt.repository,
            ctxt.pull["number"],
            "action.label",
            {"added": [], "removed": ["bar"]},
            "Rule: awesome rule",
        )
        increment.assert_called_once_with(
            "engine.signals.action.count", tags=["event:label"]
        )
