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

from mergify_engine import github_events
from mergify_engine.clients import github


def test_event_to_pull_check_run_forked_repo():
    installation_id = 12345
    owner = "CytopiaTeam"
    repo = "Cytopia"
    event_type = "check_run"

    with open(
        os.path.join(
            os.path.dirname(__file__), "events", "check_run_event_from_forked_repo.json"
        ),
        "rb",
    ) as f:
        data = json.load(f)

    client = mock.Mock(
        base_url=httpx.URL(
            "https://api.github.com/repos/CytopiaTeam/Cytopia/", allow_relative=False,
        )
    )
    client.items.return_value = []
    cm_client = mock.Mock()
    cm_client.__enter__ = mock.Mock(return_value=client)
    cm_client.__exit__ = mock.Mock()

    with mock.patch.object(github, "get_client", return_value=cm_client):
        with mock.patch.object(
            github, "get_installation", return_value={"id": installation_id}
        ):
            pulls = github_events.extract_pull_numbers_from_event(
                installation_id, owner, repo, event_type, data
            )
            assert len(pulls) == 0


def test_event_to_pull_check_run_same_repo():
    installation_id = 12345
    owner = "CytopiaTeam"
    repo = "Cytopia"
    event_type = "check_run"

    with open(
        os.path.join(
            os.path.dirname(__file__), "events", "check_run_event_from_same_repo.json",
        ),
        "rb",
    ) as f:
        data = json.load(f)

    client = mock.Mock(
        base_url=httpx.URL(
            "https://api.github.com/repos/CytopiaTeam/Cytopia/", allow_relative=False,
        )
    )
    client.items.return_value = []
    cm_client = mock.Mock()
    cm_client.__enter__ = mock.Mock(return_value=client)
    cm_client.__exit__ = mock.Mock()

    with mock.patch.object(github, "get_client", return_value=cm_client):
        with mock.patch.object(
            github, "get_installation", return_value={"id": installation_id}
        ):
            pulls = github_events.extract_pull_numbers_from_event(
                installation_id, owner, repo, event_type, data
            )
            assert len(pulls) == 1
            assert pulls[0] == 409
