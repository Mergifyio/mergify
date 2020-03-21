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

from mergify_engine.tasks import engine


def test_event_to_pull_check_run_forked_repo():
    client = mock.Mock(
        base_url=httpx.URL(
            "https://api.github.com/repos/CytopiaTeam/Cytopia/", allow_relative=False,
        )
    )
    client.items.return_value = []
    event_type = "check_run"

    with open(
        os.path.dirname(__file__) + "/check_run_event_from_forked_repo.json", "rb"
    ) as f:
        data = json.loads(f.read())

    pull = engine.get_github_pull_from_event(client, event_type, data)
    assert pull is None


def test_event_to_pull_check_run_same_repo():
    client = mock.Mock(
        base_url=httpx.URL(
            "https://api.github.com/repos/CytopiaTeam/Cytopia/", allow_relative=False,
        )
    )
    client.items.return_value = []
    event_type = "check_run"

    with open(
        os.path.dirname(__file__) + "/check_run_event_from_same_repo.json", "rb"
    ) as f:
        data = json.loads(f.read())

    pull = engine.get_github_pull_from_event(client, event_type, data)
    assert pull["number"] == 409
