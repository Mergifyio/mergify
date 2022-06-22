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
import datetime
import sys
import typing
from unittest import mock

from freezegun import freeze_time
import pytest
import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import date
from mergify_engine import delayed_refresh
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import queue
from mergify_engine import redis_utils
from mergify_engine import rules
from mergify_engine.queue import merge_train
from mergify_engine.tests.unit import conftest


async def fake_train_car_start_checking_with_draft(
    inner_self: merge_train.TrainCar, queue_rule: rules.QueueRule
) -> None:
    inner_self.creation_state = "created"
    inner_self.queue_pull_request_number = github_types.GitHubPullRequestNumber(
        inner_self.still_queued_embarked_pulls[-1].user_pull_request_number + 10
    )


async def fake_train_car_start_checking_inplace(
    inner_self: merge_train.TrainCar, queue_rule: rules.QueueRule
) -> None:
    inner_self.creation_state = "updated"


async def fake_train_car_delete_pull(
    inner_self: merge_train.TrainCar,
    reason: str,
    not_reembarked_pull_request: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = None,
) -> None:
    pass


@pytest.fixture(autouse=True)
def autoload_redis(redis_cache: redis_utils.RedisCache) -> None:
    # Just always load redis_cache to load all redis scripts
    pass


@pytest.fixture(autouse=True)
def monkepatched_traincar(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.start_checking_inplace",
        fake_train_car_start_checking_inplace,
    )

    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.start_checking_with_draft",
        fake_train_car_start_checking_with_draft,
    )
    monkeypatch.setattr(
        "mergify_engine.queue.merge_train.TrainCar.delete_pull",
        fake_train_car_delete_pull,
    )


MERGIFY_CONFIG = """
queue_rules:
  - name: inplace
    conditions: []
    speculative_checks: 5

  - name: high-1x1
    conditions: []
    speculative_checks: 1

  - name: high-1x2
    conditions: []
    speculative_checks: 1
    allow_inplace_checks: False
    batch_size: 2
    batch_max_wait_time: 0 s

  - name: 1x5
    conditions: []
    speculative_checks: 1
    allow_inplace_checks: False
    batch_size: 5
    batch_max_wait_time: 0 s

  - name: 2x1
    conditions: []
    speculative_checks: 2
    allow_inplace_checks: False
  - name: 2x5
    conditions: []
    speculative_checks: 2
    allow_inplace_checks: False
    batch_size: 5
    batch_max_wait_time: 0 s

  - name: 5x1
    conditions: []
    speculative_checks: 5
    allow_inplace_checks: False
  - name: 5x3
    conditions: []
    speculative_checks: 5
    allow_inplace_checks: False
    batch_size: 3
    batch_max_wait_time: 0 s

  - name: batch-wait-time
    conditions: []
    speculative_checks: 2
    allow_inplace_checks: False
    batch_size: 2
    batch_max_wait_time: 5 m

  - name: high-2x5-noint
    conditions: []
    speculative_checks: 2
    allow_inplace_checks: False
    batch_size: 5
    batch_max_wait_time: 0 s
    allow_checks_interruption: False
  - name: low-2x5-noint
    conditions: []
    speculative_checks: 2
    allow_inplace_checks: False
    batch_size: 5
    batch_max_wait_time: 0 s
    allow_checks_interruption: False
    allow_inplace_checks: False
  - name: low-1x5-noint
    conditions: []
    speculative_checks: 1
    batch_size: 5
    batch_max_wait_time: 0 s
    allow_checks_interruption: False
    allow_inplace_checks: False

  - name: urgent-1x4
    conditions: []
    speculative_checks: 1
    batch_size: 4
    batch_max_wait_time: 0 s
    allow_inplace_checks: False
  - name: fastlane-1x8-noint
    conditions: []
    speculative_checks: 1
    batch_size: 8
    batch_max_wait_time: 0 s
    allow_checks_interruption: False
    allow_inplace_checks: False
  - name: regular-1x8-noint-from-fastlane-and-regular
    conditions: []
    speculative_checks: 1
    batch_size: 4
    batch_max_wait_time: 0 s
    allow_inplace_checks: False
    disallow_checks_interruption_from_queues:
      - regular-1x8-noint-from-fastlane-and-regular
      - fastlane-1x8-noint

"""

QUEUE_RULES = voluptuous.Schema(rules.QueueRulesSchema)(
    rules.YamlSchema(MERGIFY_CONFIG)["queue_rules"]
)


@pytest.fixture
def fake_client() -> mock.Mock:
    branch = {"commit": {"sha": "sha1"}}

    def item_call(url, *args, **kwargs):
        if url == "/repos/Mergifyio/mergify-engine/contents/.mergify.yml":
            return {
                "type": "file",
                "sha": "whatever",
                "content": base64.b64encode(MERGIFY_CONFIG.encode()).decode(),
                "path": ".mergify.yml",
            }
        elif url == "repos/Mergifyio/mergify-engine/branches/main":
            return branch

        for i in range(40, 49):
            if url.startswith(f"/repos/Mergifyio/mergify-engine/pulls/{i}"):
                return {"merged": True, "merge_commit_sha": f"sha{i}"}

        raise Exception(f"url not mocked: {url}")

    def update_base_sha(sha):
        branch["commit"]["sha"] = sha

    client = mock.Mock()
    client.item = mock.AsyncMock(side_effect=item_call)
    client.items = mock.MagicMock()
    client.update_base_sha = update_base_sha
    return client


@pytest.fixture
def repository(
    fake_repository: context.Repository, fake_client: mock.Mock
) -> context.Repository:
    fake_repository.installation.client = fake_client
    return fake_repository


def get_cars_content(
    train: merge_train.Train,
) -> typing.List[typing.List[github_types.GitHubPullRequestNumber]]:
    cars = []
    for car in train._cars:
        cars.append(
            car.parent_pull_request_numbers
            + [ep.user_pull_request_number for ep in car.still_queued_embarked_pulls]
        )
    return cars


def get_waiting_content(
    train: merge_train.Train,
) -> typing.List[github_types.GitHubPullRequestNumber]:
    return [
        wp.user_pull_request_number for wp in train._waiting_pulls_ordered_by_priority
    ]


def get_config(queue_name: str, priority: int = 100) -> queue.PullQueueConfig:
    effective_priority = typing.cast(
        int,
        priority
        + QUEUE_RULES[queue_name].config["priority"] * queue.QUEUE_PRIORITY_OFFSET,
    )
    return queue.PullQueueConfig(
        name=rules.QueueName(queue_name),
        strict_method="merge",
        update_method="merge",
        priority=priority,
        effective_priority=effective_priority,
        bot_account=None,
        update_bot_account=None,
    )


async def test_train_inplace_with_speculative_checks_out_of_date(
    context_getter: conftest.ContextGetterFixture,
    repository: context.Repository,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("inplace")

    await t.add_pull(await context_getter(12345), config, "")
    await t.add_pull(await context_getter(54321), config, "")
    with mock.patch.object(merge_train.TrainCar, "is_behind", side_effect=[True]):
        await t.refresh()
    assert [[12345], [12345, 54321]] == get_cars_content(t)
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"


async def test_train_inplace_with_speculative_checks_up_to_date(
    context_getter: conftest.ContextGetterFixture,
    repository: context.Repository,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("inplace")

    await t.add_pull(await context_getter(12345), config, "")
    await t.add_pull(await context_getter(54321), config, "")
    with mock.patch.object(merge_train.TrainCar, "is_behind", side_effect=[False]):
        await t.refresh()
    assert [[12345], [12345, 54321]] == get_cars_content(t)
    assert t._cars[0].creation_state == "updated"
    assert t._cars[1].creation_state == "created"


async def test_train_add_pull(
    context_getter: conftest.ContextGetterFixture,
    repository: context.Repository,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1")

    await t.add_pull(await context_getter(1), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)

    await t.add_pull(await context_getter(2), config, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)

    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[1], [1, 3]] == get_cars_content(t)


async def test_train_remove_middle_merged(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1")
    await t.add_pull(await context_getter(1), config, "")
    await t.add_pull(await context_getter(2), config, "")
    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(
        await context_getter(2, merged=True, merge_commit_sha="new_sha1"), ""
    )
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)


async def test_train_remove_middle_not_merged(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("5x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("5x1", 100), "")
    await t.add_pull(await context_getter(2), get_config("5x1", 1000), "")

    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)


async def test_train_remove_head_not_merged(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1")

    await t.add_pull(await context_getter(1), config, "")
    await t.add_pull(await context_getter(2), config, "")
    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await context_getter(1), "")
    await t.refresh()
    assert [[2], [2, 3]] == get_cars_content(t)


async def test_train_remove_head_merged(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1")

    await t.add_pull(await context_getter(1), config, "")
    await t.add_pull(await context_getter(2), config, "")
    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(
        await context_getter(1, merged=True, merge_commit_sha="new_sha1"), ""
    )
    await t.refresh()
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)


async def test_train_add_remove_pull_idempotant(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1", priority=0)

    await t.add_pull(await context_getter(1), config, "")
    await t.add_pull(await context_getter(2), config, "")
    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    config = get_config("5x1", priority=10)

    await t.add_pull(await context_getter(1), config, "")
    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[1], [1, 2], [1, 2, 3]] == get_cars_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1], [1, 3]] == get_cars_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[1], [1, 3]] == get_cars_content(t)


async def test_train_mutiple_queue(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config_two = get_config("2x1", priority=0)
    config_five = get_config("5x1", priority=0)

    await t.add_pull(await context_getter(1), config_two, "")
    await t.add_pull(await context_getter(2), config_two, "")
    await t.add_pull(await context_getter(3), config_five, "")
    await t.add_pull(await context_getter(4), config_five, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)

    # Ensure we don't got over the train_size
    await t.add_pull(await context_getter(5), config_two, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4] == get_waiting_content(t)

    await t.add_pull(await context_getter(6), config_five, "")
    await t.add_pull(await context_getter(7), config_five, "")
    await t.add_pull(await context_getter(8), config_five, "")
    await t.add_pull(await context_getter(9), config_five, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [5, 3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1], [1, 5]] == get_cars_content(
        t
    ), f"{get_cars_content(t)} {get_waiting_content(t)}"
    assert [3, 4, 6, 7, 8, 9] == get_waiting_content(t)

    await t.remove_pull(await context_getter(1), "")
    await t.remove_pull(await context_getter(5), "")
    await t.refresh()
    assert [[3], [3, 4], [3, 4, 6], [3, 4, 6, 7], [3, 4, 6, 7, 8]] == get_cars_content(
        t
    )
    assert [9] == get_waiting_content(t)

    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()
    assert [[3], [3, 4], [3, 4, 6], [3, 4, 6, 7], [3, 4, 6, 7, 8]] == get_cars_content(
        t
    )
    assert [9] == get_waiting_content(t)


async def test_train_remove_duplicates(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(4), get_config("2x1", 1000), "")

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)

    # Insert bugs in queue
    t._waiting_pulls.extend(
        [
            merge_train.EmbarkedPull(
                t,
                t._cars[0].still_queued_embarked_pulls[0].user_pull_request_number,
                t._cars[0].still_queued_embarked_pulls[0].config,
                t._cars[0].still_queued_embarked_pulls[0].queued_at,
            ),
            t._waiting_pulls[0],
        ]
    )
    t._cars = t._cars + t._cars
    assert [[1], [1, 2], [1], [1, 2]] == get_cars_content(t)
    assert [1, 3, 3, 4] == get_waiting_content(t)

    # Everything should be back to normal
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)


async def test_train_remove_end_wp(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("high-1x1", 1000), "")

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await context_getter(3), "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2] == get_waiting_content(t)


async def test_train_remove_first_wp(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("high-1x1", 1000), "")

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await context_getter(2), "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)


async def test_train_remove_last_cars(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("high-1x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("high-1x1", 1000), "")

    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    await t.remove_pull(await context_getter(1), "")
    await t.refresh()
    assert [[2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)


async def test_train_with_speculative_checks_decreased(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("5x1", 1000)
    await t.add_pull(await context_getter(1), config, "")

    QUEUE_RULES["5x1"].config["speculative_checks"] = 2

    await t.add_pull(await context_getter(2), config, "")
    await t.add_pull(await context_getter(3), config, "")
    await t.add_pull(await context_getter(4), config, "")
    await t.add_pull(await context_getter(5), config, "")

    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]] == get_cars_content(
        t
    )
    assert [] == get_waiting_content(t)

    await t.remove_pull(
        await context_getter(1, merged=True, merge_commit_sha="new_sha1"), ""
    )

    with mock.patch.object(
        sys.modules[__name__],
        "MERGIFY_CONFIG",
        """
queue_rules:
  - name: 5x1
    conditions: []
    speculative_checks: 2
""",
    ):
        repository._caches.mergify_config.delete()
        repository._caches.mergify_config_file.delete()
        await repository.clear_config_file_cache(
            repository.installation.redis.cache, repository.repo["id"]
        )
        await t.refresh()
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)
    assert [4, 5] == get_waiting_content(t)


async def test_train_queue_config_change(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("2x1", 1000), "")

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    with mock.patch.object(
        sys.modules[__name__],
        "MERGIFY_CONFIG",
        """
queue_rules:
  - name: 2x1
    conditions: []
    speculative_checks: 1
""",
    ):
        repository._caches.mergify_config.delete()
        repository._caches.mergify_config_file.delete()
        await repository.clear_config_file_cache(
            repository.installation.redis.cache, repository.repo["id"]
        )
        await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_config_deleted(
    report_failure: mock.Mock,
    repository: context.Repository,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("5x1", 1000), "")

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    with mock.patch.object(
        sys.modules[__name__],
        "MERGIFY_CONFIG",
        """
queue_rules:
  - name: five
    conditions: []
    speculative_checks: 5
""",
    ):
        repository._caches.mergify_config.delete()
        repository._caches.mergify_config_file.delete()
        await repository.clear_config_file_cache(
            repository.installation.redis.cache, repository.repo["id"]
        )
        await t.refresh()
    assert [] == get_cars_content(t)
    assert [1, 2, 3] == get_waiting_content(t)
    assert len(report_failure.mock_calls) == 1


async def test_train_priority_change(
    repository: context.Repository,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    await t.add_pull(await context_getter(1), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(2), get_config("2x1", 1000), "")
    await t.add_pull(await context_getter(3), get_config("2x1", 1000), "")

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    assert (
        t._cars[0].still_queued_embarked_pulls[0].config["effective_priority"]
        == QUEUE_RULES["2x1"].config["priority"] * queue.QUEUE_PRIORITY_OFFSET + 1000
    )

    # NOTE(sileht): pull request got requeued with new configuration that don't
    # update the position but update the prio
    await t.add_pull(await context_getter(1), get_config("2x1", 2000), "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    assert (
        t._cars[0].still_queued_embarked_pulls[0].config["effective_priority"]
        == QUEUE_RULES["2x1"].config["priority"] * queue.QUEUE_PRIORITY_OFFSET + 2000
    )


def test_train_batch_split(repository: context.Repository) -> None:
    now = datetime.datetime.utcnow()
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    p1_two = merge_train.EmbarkedPull(
        t, github_types.GitHubPullRequestNumber(1), get_config("2x1"), now
    )
    p2_two = merge_train.EmbarkedPull(
        t, github_types.GitHubPullRequestNumber(2), get_config("2x1"), now
    )
    p3_two = merge_train.EmbarkedPull(
        t, github_types.GitHubPullRequestNumber(3), get_config("2x1"), now
    )
    p4_five = merge_train.EmbarkedPull(
        t, github_types.GitHubPullRequestNumber(4), get_config("5x1"), now
    )

    assert ([p1_two], [p2_two, p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "2x1", 1
    )
    assert ([p1_two, p2_two], [p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "2x1", 2
    )
    assert ([p1_two, p2_two, p3_two], [p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "2x1", 10
    )
    assert ([], [p1_two, p2_two, p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "5x1", 10
    )


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_1x2(
    report_failure: mock.Mock,
    repository: context.Repository,
    fake_client: mock.Mock,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 43):
        await t.add_pull(await context_getter(i), get_config("high-1x2", 1000), "")
    for i in range(6, 20):
        await t.add_pull(await context_getter(i), get_config("high-1x2", 1000), "")

    await t.refresh()
    assert [[41, 42]] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    assert [[41, 42]] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)

    await t.load()
    await t.refresh()
    assert [
        [41],
        [41, 42],
    ] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"

    # mark [41] as failed
    t._cars[1].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    await t.remove_pull(await context_getter(41, merged=False), "")

    # It's 41 fault, we restart the train on 42
    await t.refresh()
    assert [[42, 6]] == get_cars_content(t)
    assert list(range(7, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 0
    assert t._cars[0].creation_state == "created"


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_1x5(
    report_failure: mock.Mock,
    repository: context.Repository,
    fake_client: mock.Mock,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 46):
        await t.add_pull(await context_getter(i), get_config("1x5", 1000), "")
    for i in range(6, 20):
        await t.add_pull(await context_getter(i), get_config("1x5", 1000), "")

    await t.refresh()
    assert [[41, 42, 43, 44, 45]] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    assert [[41, 42, 43, 44, 45]] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)

    await t.load()
    await t.refresh()
    assert [
        [41, 42],
        [41, 42, 43, 44],
        [41, 42, 43, 44, 45],
    ] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "pending"
    assert t._cars[2].creation_state == "created"

    # mark [43+44] as failed
    t._cars[1].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()

    # nothing should move yet as we don't known yet if [41+42] is broken or not
    await t.refresh()
    assert [
        [41, 42],
        [41, 42, 43, 44],
        [41, 42, 43, 44, 45],
    ] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "pending"
    assert t._cars[2].creation_state == "created"

    # mark [41+42] as ready and merge it
    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()
    fake_client.update_base_sha("sha41")
    await t.remove_pull(
        await context_getter(41, merged=True, merge_commit_sha="sha41"), ""
    )
    fake_client.update_base_sha("sha42")
    await t.remove_pull(
        await context_getter(42, merged=True, merge_commit_sha="sha42"), ""
    )

    # [43+44] fail, so it's not 45, but is it 43 or 44?
    await t.refresh()
    assert [
        [41, 42, 43],
        [41, 42, 43, 44],
    ] == get_cars_content(t)
    assert [45] + list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 2
    assert len(t._cars[1].failure_history) == 1
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "pending"

    # mark [43] as failure
    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    await t.remove_pull(await context_getter(43, merged=False), "")

    # Train got cut after 43, and we restart from the begining
    await t.refresh()
    assert [[44, 45, 6, 7, 8]] == get_cars_content(t)
    assert list(range(9, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 0
    assert t._cars[0].creation_state == "created"


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_2x5(
    report_failure: mock.Mock,
    repository: context.Repository,
    fake_client: mock.Mock,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 46):
        await t.add_pull(await context_getter(i), get_config("2x5", 1000), "")
    for i in range(6, 20):
        await t.add_pull(await context_getter(i), get_config("2x5", 1000), "")

    await t.refresh()
    assert [
        [41, 42, 43, 44, 45],
        [41, 42, 43, 44, 45, 6, 7, 8, 9, 10],
    ] == get_cars_content(t)
    assert list(range(11, 20)) == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    assert [
        [41, 42, 43, 44, 45],
        [41, 42, 43, 44, 45, 6, 7, 8, 9, 10],
    ] == get_cars_content(t)
    assert list(range(11, 20)) == get_waiting_content(t)

    await t.load()
    await t.refresh()
    assert [
        [41, 42],
        [41, 42, 43, 44],
        [41, 42, 43, 44, 45],
    ] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"
    assert t._cars[2].creation_state == "created"

    # mark [43+44] as failed
    t._cars[1].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()

    # nothing should move yet as we don't known yet if [41+42] is broken or not
    await t.refresh()
    assert [
        [41, 42],
        [41, 42, 43, 44],
        [41, 42, 43, 44, 45],
    ] == get_cars_content(t)
    assert list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"
    assert t._cars[2].creation_state == "created"

    # mark [41+42] as ready and merge it
    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()
    fake_client.update_base_sha("sha41")
    await t.remove_pull(
        await context_getter(41, merged=True, merge_commit_sha="sha41"), ""
    )
    fake_client.update_base_sha("sha42")
    await t.remove_pull(
        await context_getter(42, merged=True, merge_commit_sha="sha42"), ""
    )

    # [43+44] fail, so it's not 45, but is it 43 or 44?
    await t.refresh()
    assert [
        [41, 42, 43],
        [41, 42, 43, 44],
    ] == get_cars_content(t)
    assert [45] + list(range(6, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 2
    assert len(t._cars[1].failure_history) == 1
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"

    # mark [43] as failure
    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    await t.remove_pull(await context_getter(43, merged=False), "")

    # Train got cut after 43, and we restart from the begining
    await t.refresh()
    assert [
        [44, 45, 6, 7, 8],
        [44, 45, 6, 7, 8, 9, 10, 11, 12, 13],
    ] == get_cars_content(t)
    assert list(range(14, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 0
    assert len(t._cars[1].failure_history) == 0
    assert t._cars[0].creation_state == "created"
    assert t._cars[1].creation_state == "created"


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_5x3(
    report_failure: mock.Mock,
    repository: context.Repository,
    context_getter: conftest.ContextGetterFixture,
    fake_client: mock.Mock,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 47):
        await t.add_pull(await context_getter(i), get_config("5x3", 1000), "")
    for i in range(7, 22):
        await t.add_pull(await context_getter(i), get_config("5x3", 1000), "")

    await t.refresh()
    assert [
        [41, 42, 43],
        [41, 42, 43, 44, 45, 46],
        [41, 42, 43, 44, 45, 46, 7, 8, 9],
        [41, 42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12],
        [41, 42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    ] == get_cars_content(t)
    assert list(range(16, 22)) == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    assert [
        [41, 42, 43],
        [41, 42, 43, 44, 45, 46],
        [41, 42, 43, 44, 45, 46, 7, 8, 9],
        [41, 42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12],
        [41, 42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    ] == get_cars_content(t)
    assert list(range(16, 22)) == get_waiting_content(t)

    await t.load()
    await t.refresh()
    assert [
        [41],
        [41, 42],
        [41, 42, 43],
    ] == get_cars_content(t)
    assert [44, 45, 46] + list(range(7, 22)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0

    # mark [41] as failed
    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    await t.remove_pull(await context_getter(41, merged=False), "")

    # nothing should move yet as we don't known yet if [41+42] is broken or not
    await t.refresh()
    assert [
        [42, 43, 44],
        [42, 43, 44, 45, 46, 7],
        [42, 43, 44, 45, 46, 7, 8, 9, 10],
        [42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12, 13],
        [42, 43, 44, 45, 46, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    ] == get_cars_content(t)
    assert list(range(17, 22)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 0
    assert len(t._cars[1].failure_history) == 0
    assert len(t._cars[2].failure_history) == 0
    assert len(t._cars[3].failure_history) == 0
    assert len(t._cars[4].failure_history) == 0

    # mark [42+43+44] as ready and merge it
    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    t._cars[1].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    fake_client.update_base_sha("sha42")
    await t.remove_pull(
        await context_getter(42, merged=True, merge_commit_sha="sha42"), ""
    )
    fake_client.update_base_sha("sha43")
    await t.remove_pull(
        await context_getter(43, merged=True, merge_commit_sha="sha43"), ""
    )
    fake_client.update_base_sha("sha44")
    await t.remove_pull(
        await context_getter(44, merged=True, merge_commit_sha="sha44"), ""
    )

    await t.refresh()
    assert [
        [42, 43, 44, 45],
        [42, 43, 44, 45, 46],
        [42, 43, 44, 45, 46, 7],
    ] == get_cars_content(t)
    assert list(range(8, 22)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0

    # mark [45] and [46+46] as success, so it's 7 fault !
    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    t._cars[1].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()

    # Nothing change yet!
    await t.refresh()
    assert [
        [42, 43, 44, 45],
        [42, 43, 44, 45, 46],
        [42, 43, 44, 45, 46, 7],
    ] == get_cars_content(t)
    assert list(range(8, 22)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 1
    assert len(t._cars[1].failure_history) == 1
    assert len(t._cars[2].failure_history) == 0
    # Merge 45 and 46
    fake_client.update_base_sha("sha45")
    await t.remove_pull(
        await context_getter(45, merged=True, merge_commit_sha="sha45"), ""
    )
    fake_client.update_base_sha("sha46")
    await t.remove_pull(
        await context_getter(46, merged=True, merge_commit_sha="sha46"), ""
    )
    await t.refresh()
    assert [
        [42, 43, 44, 45, 46, 7],
    ] == get_cars_content(t)
    assert t._cars[0].checks_conclusion == check_api.Conclusion.FAILURE
    assert len(t._cars[0].failure_history) == 0

    # remove the failed 7
    await t.remove_pull(await context_getter(7, merged=False), "")

    # Train got cut after 43, and we restart from the begining
    await t.refresh()
    assert [
        [8, 9, 10],
        [8, 9, 10, 11, 12, 13],
        [8, 9, 10, 11, 12, 13, 14, 15, 16],
        [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
        [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
    ] == get_cars_content(t)
    assert [] == get_waiting_content(t)


async def test_train_no_interrupt_add_pull(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("high-2x5-noint")

    await t.add_pull(await context_getter(1), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(2), config, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    # Inserting high prio didn't break started speculative checks, but the PR
    # move above other
    await t.add_pull(await context_getter(4), get_config("high-2x5-noint", 20000), "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [4, 3] == get_waiting_content(t)


async def test_train_always_interrupt_across_queue(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("low-2x5-noint")

    await t.add_pull(await context_getter(1), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(2), config, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    # Inserting pr in high queue always break started speculative checks even
    # if allow_checks_interruption is set
    await t.add_pull(await context_getter(4), get_config("high-2x5-noint"), "")
    await t.refresh()
    assert [[4]] == get_cars_content(t)
    assert [1, 2, 3] == get_waiting_content(t)


async def test_train_interrupt_mixed_across_queue(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    config = get_config("low-1x5-noint")

    await t.add_pull(await context_getter(1), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(2), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2] == get_waiting_content(t)

    await t.add_pull(await context_getter(3), config, "")
    await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)

    # Inserting pr in high queue always break started speculative checks
    await t.add_pull(await context_getter(4), get_config("high-1x2"), "")
    await t.refresh()
    assert [[4]] == get_cars_content(t)
    assert [1, 2, 3] == get_waiting_content(t)


async def test_train_disallow_checks_interruption_scenario_1(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    urgent = get_config("urgent-1x4")
    fastlane = get_config("fastlane-1x8-noint")
    regular = get_config("regular-1x8-noint-from-fastlane-and-regular")

    await t.add_pull(await context_getter(1), fastlane, "")
    await t.add_pull(await context_getter(2), fastlane, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    # regular doesn't interrupt the checks as it's below fastlane
    await t.add_pull(await context_getter(3), regular, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    # fastlane doesn't interrupt the checks because of noint, but goes before
    # regular
    await t.add_pull(await context_getter(4), fastlane, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [4, 3] == get_waiting_content(t)

    # urgent breaks everything, and all fastlane got pack together, regular move behind
    await t.add_pull(await context_getter(5), urgent, "")
    await t.refresh()
    assert [[5]] == get_cars_content(t)
    assert [1, 2, 4, 3] == get_waiting_content(t)


async def test_train_disallow_checks_interruption_scenario_2(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    urgent = get_config("urgent-1x4")
    fastlane = get_config("fastlane-1x8-noint")
    regular = get_config("regular-1x8-noint-from-fastlane-and-regular")

    await t.add_pull(await context_getter(1), regular, "")
    await t.add_pull(await context_getter(2), regular, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    # fastlane doesn't interrupt the checks as
    # disallow_checks_interruption_from_queues of regular disallow it
    await t.add_pull(await context_getter(3), fastlane, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    # fastlane doesn't interrupt the checks because of noint, but goes before
    # regular
    await t.add_pull(await context_getter(4), regular, "")
    await t.refresh()
    assert [[1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)

    # urgent breaks everything, then we put the fastlane one, and all regulars goes behind
    await t.add_pull(await context_getter(5), urgent, "")
    await t.refresh()
    assert [[5]] == get_cars_content(t)
    assert [3, 1, 2, 4] == get_waiting_content(t)


async def test_train_batch_max_wait_time(
    repository: context.Repository, context_getter: conftest.ContextGetterFixture
) -> None:
    with freeze_time("2021-09-22T08:00:00") as freezed_time:
        t = merge_train.Train(repository, github_types.GitHubRefType("main"))
        await t.load()

        config = get_config("batch-wait-time")

        await t.add_pull(await context_getter(1), config, "")
        await t.refresh()
        assert [] == get_cars_content(t)
        assert [1] == get_waiting_content(t)

        # Enought PR to batch!
        await t.add_pull(await context_getter(2), config, "")
        await t.refresh()
        assert [[1, 2]] == get_cars_content(t)
        assert [] == get_waiting_content(t)

        await t.add_pull(await context_getter(3), config, "")
        await t.refresh()
        assert [[1, 2]] == get_cars_content(t)
        assert [3] == get_waiting_content(t)

        d = await delayed_refresh._get_current_refresh_datetime(
            repository, github_types.GitHubPullRequestNumber(3)
        )
        assert d is not None
        assert d == freezed_time().replace(
            tzinfo=datetime.timezone.utc
        ) + datetime.timedelta(minutes=5)

    with freeze_time("2021-09-22T08:05:02"):
        await t.refresh()
        assert [[1, 2], [1, 2, 3]] == get_cars_content(t)
        assert [] == get_waiting_content(t)


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_pr_with_higher_prio_enters_in_queue_during_merging_1x5(
    report_failure: mock.Mock,
    repository: context.Repository,
    context_getter: conftest.ContextGetterFixture,
    fake_client: mock.Mock,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 46):
        await t.add_pull(await context_getter(i), get_config("1x5", 1000), "")

    await t.refresh()
    assert [[41, 42, 43, 44, 45]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()
    await t.refresh()
    assert [[41, 42, 43, 44, 45]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    # merge half of the batch
    for i in range(41, 44):
        fake_client.update_base_sha(f"sha{i}")
        await t.remove_pull(
            await context_getter(i, merged=True, merge_commit_sha=f"sha{i}"), ""
        )

    await t.refresh()
    assert [[44, 45]] == get_cars_content(t)
    assert [] == get_waiting_content(t)

    await t.add_pull(await context_getter(7), get_config("1x5", 10000), "")
    await t.refresh()
    assert [[44, 45]] == get_cars_content(t)
    assert [7] == get_waiting_content(t)


@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_pr_with_higher_prio_enters_in_queue_during_merging_2x5(
    report_failure: mock.Mock,
    repository: context.Repository,
    context_getter: conftest.ContextGetterFixture,
    fake_client: mock.Mock,
) -> None:
    t = merge_train.Train(repository, github_types.GitHubRefType("main"))
    await t.load()

    for i in range(41, 52):
        await t.add_pull(await context_getter(i), get_config("2x5", 1000), "")

    await t.refresh()
    assert [
        [41, 42, 43, 44, 45],
        [41, 42, 43, 44, 45, 46, 47, 48, 49, 50],
    ] == get_cars_content(t)
    assert [51] == get_waiting_content(t)

    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()
    await t.refresh()
    assert [
        [41, 42, 43, 44, 45],
        [41, 42, 43, 44, 45, 46, 47, 48, 49, 50],
    ] == get_cars_content(t)
    assert [51] == get_waiting_content(t)

    # merge half of the batch
    for i in range(41, 44):
        fake_client.update_base_sha(f"sha{i}")
        await t.remove_pull(
            await context_getter(i, merged=True, merge_commit_sha=f"sha{i}"), ""
        )

    await t.refresh()
    assert [
        [44, 45],
        [41, 42, 43, 44, 45, 46, 47, 48, 49, 50],
    ] == get_cars_content(t)
    assert [51] == get_waiting_content(t)

    await t.add_pull(await context_getter(7), get_config("2x5", 2000), "")

    await t.refresh()
    assert [[44, 45], [44, 45, 7, 46, 47, 48, 49]] == get_cars_content(t)
    assert [50, 51] == get_waiting_content(t)


def test_embarked_pull_old_serialization() -> None:
    config = queue.PullQueueConfig(
        name=rules.QueueName("foo"),
        strict_method="merge",
        update_method="merge",
        priority=0,
        effective_priority=0,
        bot_account=None,
        update_bot_account=None,
    )

    now = date.utcnow()
    old_typed = merge_train.EmbarkedPull.OldSerialized(
        github_types.GitHubPullRequestNumber(1234), config, now
    )
    old_untyped = json.loads(json.dumps(old_typed))
    ep = merge_train.EmbarkedPull.deserialize(mock.Mock(), old_untyped)
    assert ep.user_pull_request_number == 1234
    assert ep.config == config
    assert ep.queued_at == now
