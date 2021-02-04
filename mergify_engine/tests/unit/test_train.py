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
from mergify_engine import subscription


async def fake_train_car_create_pull(inner_self):
    inner_self.queue_pull_request_number = inner_self.user_pull_request_number + 10


async def fake_train_car_delete_pull(inner_self):
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
    client.item = mock.AsyncMock(return_value={"commit": {"sha": "sha1"}})
    return client


async def fake_context(repository, number, **kwargs):
    pull: github_types.GitHubPullRequest = {
        "title": "awesome",
        "id": 123,
        "maintainer_can_modify": True,
        "user": {
            "id": 123,
            "type": "Orgs",
            "login": "Mergifyio",
            "avatar_url": "",
        },
        "labels": [],
        "rebaseable": True,
        "draft": False,
        "merge_commit_sha": None,
        "number": number,
        "mergeable_state": "clean",
        "state": "open",
        "changed_files": 1,
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
                "owner": {
                    "id": 123,
                    "type": "Orgs",
                    "login": "Mergifyio",
                    "avatar_url": "",
                },
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
            },
            "user": {
                "id": 123,
                "type": "Orgs",
                "login": "Mergifyio",
                "avatar_url": "",
            },
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
                "owner": {
                    "id": 123,
                    "type": "Orgs",
                    "login": "Mergifyio",
                    "avatar_url": "",
                },
                "url": "https://api.github.com/repos/Mergifyio/mergify-engine",
            },
            "sha": "miaou",
            "user": {
                "id": 123,
                "type": "Orgs",
                "login": "Mergifyio",
                "avatar_url": "",
            },
        },
    }
    pull.update(kwargs)
    return await context.Context.create(repository, pull)


def get_cars_content(train):
    cars = []
    for car in train._cars:
        cars.append(car.parent_pull_request_numbers + [car.user_pull_request_number])
    return cars


@pytest.fixture
def repository(redis_cache, fake_client):
    installation = context.Installation(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user"),
        subscription.Subscription(redis_cache, 0, False, "", {}, frozenset()),
        fake_client,
        redis_cache,
    )
    return context.Repository(
        installation,
        github_types.GitHubRepositoryName("name"),
        github_types.GitHubRepositoryIdType(123),
    )


@pytest.mark.asyncio
async def test_train_add_pull(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.insert_pull_at(await fake_context(repository, 1), 0, "foo")
    await t.refresh()
    assert [[1]] == get_cars_content(t)

    await t.insert_pull_at(await fake_context(repository, 2), 1, "foo")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)

    await t.insert_pull_at(await fake_context(repository, 3), 2, "foo")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[1], [1, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_middle_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.insert_pull_at(await fake_context(repository, 1), 0, "foo")
    await t.insert_pull_at(await fake_context(repository, 2), 1, "foo")
    await t.insert_pull_at(await fake_context(repository, 3), 2, "foo")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(
        await fake_context(repository, 2, merged=True, merge_commit_sha="new_sha1")
    )
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_middle_not_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.insert_pull_at(await fake_context(repository, 1), 0, "foo")
    await t.insert_pull_at(await fake_context(repository, 3), 1, "foo")
    await t.insert_pull_at(await fake_context(repository, 2), 1, "foo")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_head_not_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.insert_pull_at(await fake_context(repository, 1), 0, "foo")
    await t.insert_pull_at(await fake_context(repository, 2), 1, "foo")
    await t.insert_pull_at(await fake_context(repository, 3), 2, "foo")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 1))
    await t.refresh()
    assert [[2], [2, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_head_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.insert_pull_at(await fake_context(repository, 1), 0, "foo")
    await t.insert_pull_at(await fake_context(repository, 2), 1, "foo")
    await t.insert_pull_at(await fake_context(repository, 3), 2, "foo")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(
        await fake_context(repository, 1, merged=True, merge_commit_sha="new_sha1")
    )
    await t.refresh()
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)
