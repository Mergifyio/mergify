# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mehdi Abaakouk <sileht@mergify.io>
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

import json
import os
from unittest import mock

import pytest

from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import utils


async def _do_test_event_to_pull_check_run(redis_cache, filename, expected_pulls):
    owner = "CytopiaTeam"
    repo = "Cytopia"
    event_type = "check_run"

    with open(
        os.path.join(os.path.dirname(__file__), "events", filename),
        "r",
    ) as f:
        data = json.loads(
            f.read()
            .replace("https://github.com", config.GITHUB_URL)
            .replace("https://api.github.com", config.GITHUB_API_URL)
        )

    installation = context.Installation(123, owner, {}, mock.Mock(), redis_cache)
    pulls = await github_events.extract_pull_numbers_from_event(
        installation, repo, event_type, data, []
    )
    assert pulls == expected_pulls


@pytest.mark.asyncio
async def test_event_to_pull_check_run_forked_repo(redis_cache):
    await _do_test_event_to_pull_check_run(
        redis_cache, "check_run_event_from_forked_repo.json", []
    )


@pytest.mark.asyncio
async def test_event_to_pull_check_run_same_repo(redis_cache):
    await _do_test_event_to_pull_check_run(
        redis_cache, "check_run_event_from_same_repo.json", [409]
    )


GITHUB_SAMPLE_EVENTS = {}
_EVENT_DIR = os.path.join(os.path.dirname(__file__), "events")
for filename in os.listdir(_EVENT_DIR):
    event_type = filename.split(".")[0]
    with open(os.path.join(_EVENT_DIR, filename), "r") as event:
        GITHUB_SAMPLE_EVENTS[filename] = (event_type, json.load(event))


@pytest.mark.parametrize("event_type, event", list(GITHUB_SAMPLE_EVENTS.values()))
@mock.patch("mergify_engine.worker.push")
@pytest.mark.asyncio
async def test_filter_and_dispatch(
    worker_push: mock.Mock,
    event_type: github_types.GitHubEventType,
    event: github_types.GitHubEvent,
    redis_cache: utils.RedisCache,
    redis_stream: utils.RedisStream,
) -> None:
    event_id = "my_event_id"
    try:
        await github_events.filter_and_dispatch(
            redis_cache,
            redis_stream,
            event_type,
            event_id,
            event,
        )
    except github_events.IgnoredEvent as e:
        assert e.event_type == event_type
        assert e.event_id == event_id
        assert isinstance(e.reason, str)
