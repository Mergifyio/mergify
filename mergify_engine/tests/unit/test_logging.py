# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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
import importlib
import json
import logging
import typing
from unittest import mock

from ddtrace import tracer
import pytest

from mergify_engine import logs


@pytest.fixture
def enable_tracer():
    enabled = tracer.enabled
    with mock.patch.object(tracer._writer, "flush_queue"), mock.patch.object(
        tracer._writer, "write"
    ):
        tracer.enabled = True
        try:
            yield
        finally:
            tracer.enabled = enabled


def test_logging(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    enable_tracer: typing.Literal[None],
) -> None:
    monkeypatch.setenv("DYNO", "worker-shared.1")
    monkeypatch.setenv("HEROKU_RELEASE_VERSION", "v1234")
    monkeypatch.setenv("HEROKU_SLUG_COMMIT", "75a50b499de27757f171bac717c81685d648d3a7")

    importlib.reload(logs)

    logs.WORKER_ID.set("shared-30")

    with tracer.trace(
        "testing", span_type="test", resource="test_logging", service="whatever"
    ) as span:
        span.set_tag("gh_owner", "foobar")
        record = logging.LogRecord(
            "name",
            logging.ERROR,
            "file.py",
            123,
            "this is impossible",
            (),
            None,
        )
        formatter = logs.HerokuDatadogFormatter()
        formatted = formatter.format(record)
        assert json.loads(formatted) == {
            "message": "this is impossible",
            "timestamp": mock.ANY,
            "status": "error",
            "logger": {"name": "name"},
            "dd.trace_id": mock.ANY,
            "dd.span_id": mock.ANY,
            # FIXME(sileht): It should be "whatever" but it's buggy
            # here we get a random service depending on the order tests are
            # running, so use ANY for now.
            "dd.service": mock.ANY,
            "dd.version": mock.ANY,
            "dd.env": "",
            "HEROKU_RELEASE_VERSION": "v1234",
            "HEROKU_SLUG_COMMIT": "75a50b499de27757f171bac717c81685d648d3a7",
            "worker_id": "shared-30",
            "dyno": "worker-shared.1",
            "dynotype": "worker-shared",
        }
