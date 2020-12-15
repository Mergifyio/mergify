# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import merge_train
from mergify_engine import utils


@pytest.fixture()
def redis():
    r = utils.get_redis_for_cache()
    r.flushdb()
    try:
        yield r
    finally:
        r.flushdb()


def fake_train_car_create_pull(inner_self):
    inner_self.tmp_pull_request_number = inner_self.pull_request_number + 10


def fake_train_car_delete_pull(inner_self):
    pass


@pytest.fixture
def monkepatched_traincar(monkeypatch):
    monkeypatch.setattr(
        "mergify_engine.merge_train.TrainCar.create_pull", fake_train_car_create_pull
    )
    monkeypatch.setattr(
        "mergify_engine.merge_train.TrainCar.delete_pull", fake_train_car_delete_pull
    )


@pytest.fixture
def fake_client():
    client = mock.Mock()
    client.get.return_value = mock.Mock(
        json=mock.Mock(return_value={"commit": {"sha": "sha1"}})
    )
    return client


def fake_context(number, **kwargs):
    pull: github_types.GitHubPullRequest = {
        "id": 123,
        "maintainer_can_modify": True,
        "user": {"id": 123, "type": "Orgs", "login": "Mergifyio"},
        "labels": [],
        "rebaseable": True,
        "draft": False,
        "merge_commit_sha": None,
        "number": number,
        "mergeable_state": "clean",
        "state": "open",
        "head": {
            "sha": "azertyu",
            "label": "Mergifyio:feature-branch",
            "ref": "feature-branch",
            "repo": {
                "id": 123,
                "default_branch": "master",
                "name": "mergify-engine",
                "full_name": "Mergifyio/mergify-engine",
                "archived": False,
                "private": False,
                "owner": {"id": 123, "type": "Orgs", "login": "Mergifyio"},
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
            },
            "user": {"id": 123, "type": "Orgs", "login": "Mergifyio"},
        },
        "merged": False,
        "merged_by": None,
        "merged_at": None,
        "html_url": "https://...",
        "base": {
            "label": "Mergifyio:master",
            "ref": "master",
            "repo": {
                "id": 123,
                "default_branch": "master",
                "name": "mergify-engine",
                "full_name": "Mergifyio/mergify-engine",
                "archived": False,
                "private": False,
                "owner": {"id": 123, "type": "Orgs", "login": "Mergifyio"},
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
            },
            "sha": "miaou",
            "user": {"id": 123, "type": "Orgs", "login": "Mergifyio"},
        },
    }
    pull.update(kwargs)
    client = mock.MagicMock()
    return context.Context(
        client,
        pull,
        {},
    )


def get_cars_content(train):
    cars = []
    for car in train._cars:
        cars.append(car.parents + [car.pull_request_number])
    return cars


def test_train_add_pull(redis, fake_client, monkepatched_traincar):
    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )

    t.insert_pull_at(fake_context(1), 0, "foo")
    assert [[1]] == get_cars_content(t)

    t.insert_pull_at(fake_context(2), 1, "foo")
    assert [[1], [1, 2]] == get_cars_content(t)

    t.insert_pull_at(fake_context(3), 2, "foo")
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t.remove_pull(fake_context(2))
    assert [[1], [1, 3]] == get_cars_content(t)

    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )
    assert [[1], [1, 3]] == get_cars_content(t)


def test_train_remove_middle_merged(redis, fake_client, monkepatched_traincar):
    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )

    t.insert_pull_at(fake_context(1), 0, "foo")
    t.insert_pull_at(fake_context(2), 1, "foo")
    t.insert_pull_at(fake_context(3), 2, "foo")
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t.remove_pull(fake_context(2, merged=True, merge_commit_sha="new_sha1"))
    assert [[1], [1, 3]] == get_cars_content(t)


def test_train_remove_middle_not_merged(redis, fake_client, monkepatched_traincar):
    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )

    t.insert_pull_at(fake_context(1), 0, "foo")
    t.insert_pull_at(fake_context(3), 1, "foo")
    t.insert_pull_at(fake_context(2), 1, "foo")
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t.remove_pull(fake_context(2))
    assert [[1], [1, 3]] == get_cars_content(t)


def test_train_remove_head_not_merged(redis, fake_client, monkepatched_traincar):
    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )

    t.insert_pull_at(fake_context(1), 0, "foo")
    t.insert_pull_at(fake_context(2), 1, "foo")
    t.insert_pull_at(fake_context(3), 2, "foo")
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t.remove_pull(fake_context(1))
    assert [[2], [2, 3]] == get_cars_content(t)


def test_train_remove_head_merged(redis, fake_client, monkepatched_traincar):
    t = merge_train.Train(
        fake_client, redis, 123, "Mergifyio", 123, "mergify-engine", "branch"
    )

    t.insert_pull_at(fake_context(1), 0, "foo")
    t.insert_pull_at(fake_context(2), 1, "foo")
    t.insert_pull_at(fake_context(3), 2, "foo")
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t.remove_pull(fake_context(1, merged=True, merge_commit_sha="new_sha1"))
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)
