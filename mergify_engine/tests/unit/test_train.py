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

import base64
import typing
from unittest import mock

import pytest
import voluptuous

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine.actions import merge_base
from mergify_engine.queue import merge_train


async def fake_train_car_create_pull(inner_self, queue_rule):
    inner_self.queue_pull_request_number = inner_self.user_pull_request_number + 10


async def fake_train_car_update_user_pull(inner_self, queue_rule):
    pass


async def fake_train_car_delete_pull(inner_self):
    pass


@pytest.fixture
def monkepatched_traincar(monkeypatch):
    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.update_user_pull",
        fake_train_car_update_user_pull,
    )

    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.create_pull",
        fake_train_car_create_pull,
    )
    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.delete_pull",
        fake_train_car_delete_pull,
    )


MERGIFY_CONFIG = """
queue_rules:
  - name: one
    conditions: []
    speculative_checks: 1
  - name: two
    conditions: []
    speculative_checks: 2
  - name: five
    conditions: []
    speculative_checks: 5
"""

QUEUE_RULES = voluptuous.Schema(rules.QueueRulesSchema)(
    rules.YamlSchema(MERGIFY_CONFIG)["queue_rules"]
)


@pytest.fixture
def fake_client():
    def item_call(url, *args, **kwargs):
        if url == "/repos/user/name/contents/.mergify.yml":
            return {
                "type": "file",
                "sha": "whatever",
                "content": base64.b64encode(MERGIFY_CONFIG.encode()).decode(),
                "path": ".mergify.yml",
            }
        elif url == "repos/user/name/branches/branch":
            return {"commit": {"sha": "sha1"}}
        else:
            raise Exception(f"url not mocked: {url}")

    client = mock.Mock()
    client.item = mock.AsyncMock(side_effect=item_call)
    return client


async def fake_context(repository, number, **kwargs):
    pull: github_types.GitHubPullRequest = {
        "locked": False,
        "assignees": [],
        "requested_reviewers": [],
        "requested_teams": [],
        "milestone": None,
        "title": "awesome",
        "body": "",
        "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
        "closed_at": None,
        "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
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
        "commits": 1,
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
                "html_url": "https://github.com/Mergifyio/mergify-engine",
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
                "html_url": "https://github.com/Mergifyio/mergify-engine",
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


def get_waiting_content(train):
    return [wp.user_pull_request_number for wp in train._waiting_pulls]


@pytest.fixture
def repository(redis_cache, fake_client):
    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "user/name",
            "name": github_types.GitHubRepositoryName("name"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )
    installation = context.Installation(
        github_types.GitHubAccountIdType(123),
        github_types.GitHubLogin("user"),
        subscription.Subscription(redis_cache, 0, False, "", frozenset()),
        fake_client,
        redis_cache,
    )
    return context.Repository(installation, gh_repo)


def get_config(
    queue_name: rules.QueueName, priority: int = 100
) -> queue.PullQueueConfig:
    effective_priority = typing.cast(
        int,
        priority
        + QUEUE_RULES[queue_name].config["priority"] * merge_base.QUEUE_PRIORITY_OFFSET,
    )
    return queue.PullQueueConfig(
        name=queue_name,
        strict_method="merge",
        priority=priority,
        effective_priority=effective_priority,
        bot_account=None,
        update_bot_account=None,
        queue_config=QUEUE_RULES[queue_name].config,
    )


@pytest.mark.asyncio
async def test_train_add_pull(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config = get_config("five")

    await t.add_pull(await fake_context(repository, 1), config)
    await t.refresh()
    assert [[1]] == get_cars_content(t)

    await t.add_pull(await fake_context(repository, 2), config)
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)

    await t.add_pull(await fake_context(repository, 3), config)
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

    config = get_config("five")
    await t.add_pull(await fake_context(repository, 1), config)
    await t.add_pull(await fake_context(repository, 2), config)
    await t.add_pull(await fake_context(repository, 3), config)
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

    await t.add_pull(await fake_context(repository, 1), get_config("five", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("five", 100))
    await t.add_pull(await fake_context(repository, 2), get_config("five", 1000))

    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_head_not_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config = get_config("five")

    await t.add_pull(await fake_context(repository, 1), config)
    await t.add_pull(await fake_context(repository, 2), config)
    await t.add_pull(await fake_context(repository, 3), config)
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 1))
    await t.refresh()
    assert [[2], [2, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_remove_head_merged(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config = get_config("five")

    await t.add_pull(await fake_context(repository, 1), config)
    await t.add_pull(await fake_context(repository, 2), config)
    await t.add_pull(await fake_context(repository, 3), config)
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(
        await fake_context(repository, 1, merged=True, merge_commit_sha="new_sha1")
    )
    await t.refresh()
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_add_remove_pull_idempotant(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config = get_config("five", priority=0)

    await t.add_pull(await fake_context(repository, 1), config)
    await t.add_pull(await fake_context(repository, 2), config)
    await t.add_pull(await fake_context(repository, 3), config)
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    config = get_config("five", priority=10)

    await t.add_pull(await fake_context(repository, 1), config)
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[1], [1, 3]] == get_cars_content(t)


@pytest.mark.asyncio
async def test_train_mutiple_queue(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config_two = get_config("two", priority=0)
    config_five = get_config("five", priority=0)

    await t.add_pull(await fake_context(repository, 1), config_two)
    await t.add_pull(await fake_context(repository, 2), config_two)
    await t.add_pull(await fake_context(repository, 3), config_five)
    await t.add_pull(await fake_context(repository, 4), config_five)
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)

    # Ensure we don't got over the train_size
    await t.add_pull(await fake_context(repository, 5), config_two)
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4] == get_waiting_content(t)

    await t.add_pull(await fake_context(repository, 6), config_five)
    await t.add_pull(await fake_context(repository, 7), config_five)
    await t.add_pull(await fake_context(repository, 8), config_five)
    await t.add_pull(await fake_context(repository, 9), config_five)
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1], [1, 5]] == get_cars_content(t)
    assert [3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    await t.remove_pull(await fake_context(repository, 1))
    await t.remove_pull(await fake_context(repository, 5))
    await t.refresh()
    assert [[3], [3, 4], [3, 4, 6], [3, 4, 6, 7], [3, 4, 6, 7, 8]] == get_cars_content(
        t
    )
    assert [9] == get_waiting_content(t)

    t = merge_train.Train(repository, "branch")
    await t.load()
    assert [[3], [3, 4], [3, 4, 6], [3, 4, 6, 7], [3, 4, 6, 7, 8]] == get_cars_content(
        t
    )
    assert [9] == get_waiting_content(t)


@pytest.mark.asyncio
async def test_train_remove_end_wp(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("one", 1000))

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await fake_context(repository, 3))
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2] == get_waiting_content(t)


@pytest.mark.asyncio
async def test_train_remove_first_wp(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("one", 1000))

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await fake_context(repository, 2))
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)


@pytest.mark.asyncio
async def test_train_remove_last_cars(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("one", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("one", 1000))

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await fake_context(repository, 1))
    await t.refresh()
    assert [[2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)
