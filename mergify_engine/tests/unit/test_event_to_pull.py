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

import httpx
import pytest

from mergify_engine import github_events


async def _do_test_event_to_pull_check_run(filename, expected_pulls):

    installation_id = 12345
    owner = "CytopiaTeam"
    repo = "Cytopia"
    event_type = "check_run"

    with open(
        os.path.join(os.path.dirname(__file__), "events", filename),
        "rb",
    ) as f:
        data = json.load(f)

    client = mock.Mock(
        base_url=httpx.URL("https://api.github.com/"),
        name="foo",
        owner=owner,
        repo=repo,
        auth=mock.Mock(installation={"id": installation_id}, owner=owner, repo=repo),
    )
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock()
    client.items.return_value = mock.AsyncMock()

    pulls = await github_events.extract_pull_numbers_from_event(
        client, repo, event_type, data
    )
    assert pulls == expected_pulls


@pytest.mark.asyncio
async def test_event_to_pull_check_run_forked_repo():
    await _do_test_event_to_pull_check_run("check_run_event_from_forked_repo.json", [])


@pytest.mark.asyncio
async def test_event_to_pull_check_run_same_repo():
    await _do_test_event_to_pull_check_run("check_run_event_from_same_repo.json", [409])
