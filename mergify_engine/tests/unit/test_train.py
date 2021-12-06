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

import pytest
import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine.dashboard import subscription
from mergify_engine.queue import merge_train


async def fake_train_car_create_pull(inner_self, queue_rule):
    inner_self.queue_pull_request_number = (
        inner_self.still_queued_embarked_pulls[-1].user_pull_request_number + 10
    )


async def fake_train_car_update_user_pull(inner_self, queue_rule):
    pass


async def fake_train_car_delete_pull(inner_self, reason):
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
  - name: 5x3
    conditions: []
    speculative_checks: 5
    batch_size: 3
  - name: 2x5
    conditions: []
    speculative_checks: 2
    batch_size: 5

"""

QUEUE_RULES = voluptuous.Schema(rules.QueueRulesSchema)(
    rules.YamlSchema(MERGIFY_CONFIG)["queue_rules"]
)


@pytest.fixture
def fake_client():
    branch = {"commit": {"sha": "sha1"}}

    def item_call(url, *args, **kwargs):
        if url == "/repos/user/name/contents/.mergify.yml":
            return {
                "type": "file",
                "sha": "whatever",
                "content": base64.b64encode(MERGIFY_CONFIG.encode()).decode(),
                "path": ".mergify.yml",
            }
        elif url == "repos/user/name/branches/branch":
            return branch

        for i in range(40, 49):
            if url.startswith(f"/repos/user/name/pulls/{i}"):
                return {"merged": True, "merge_commit_sha": f"sha{i}"}

        raise Exception(f"url not mocked: {url}")

    def update_base_sha(sha):
        branch["commit"]["sha"] = sha

    client = mock.Mock()
    client.item = mock.AsyncMock(side_effect=item_call)
    client.update_base_sha = update_base_sha
    return client


async def fake_context(repository, number, **kwargs):
    pull: github_types.GitHubPullRequest = {
        "node_id": "42",
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
        "mergeable": True,
        "state": "open",
        "changed_files": 1,
        "head": {
            "sha": "azertyu",
            "label": "Mergifyio:feature-branch",
            "ref": "feature-branch",
            "repo": {
                "id": 123,
                "default_branch": "main",
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
            "label": "Mergifyio:branch",
            "ref": "branch",
            "repo": repository.repo,
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
        cars.append(
            car.parent_pull_request_numbers
            + [ep.user_pull_request_number for ep in car.still_queued_embarked_pulls]
        )
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
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
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
        + QUEUE_RULES[queue_name].config["priority"] * queue.QUEUE_PRIORITY_OFFSET,
    )
    return queue.PullQueueConfig(
        name=queue_name,
        strict_method="merge",
        update_method="merge",
        priority=priority,
        effective_priority=effective_priority,
        bot_account=None,
        update_bot_account=None,
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
    assert [[1], [1, 5]] == get_cars_content(
        t
    ), f"{get_cars_content(t)} {get_waiting_content(t)}"
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
async def test_train_remove_duplicates(repository, monkepatched_traincar):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 4), get_config("two", 1000))

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)

    # Insert bugs in queue
    t._waiting_pulls.extend(
        [
            merge_train.EmbarkedPull(
                t._cars[0].still_queued_embarked_pulls[0].user_pull_request_number,
                t._cars[0].still_queued_embarked_pulls[0].config,
                t._cars[0].still_queued_embarked_pulls[0].queued_at,
            ),
            t._waiting_pulls[0],
        ]
    )
    t._cars = t._cars + t._cars
    assert [[1], [1, 2], [1], [1, 2]] == get_cars_content(t)
    assert [3, 4, 1, 3] == get_waiting_content(t)

    # Everything should be back to normal
    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3, 4] == get_waiting_content(t)


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


@pytest.mark.asyncio
async def test_train_with_speculative_checks_decreased(
    repository, monkepatched_traincar
):
    t = merge_train.Train(repository, "branch")
    await t.load()

    config = get_config("five", 1000)
    await t.add_pull(await fake_context(repository, 1), config)

    QUEUE_RULES["five"].config["speculative_checks"] = 2

    await t.add_pull(await fake_context(repository, 2), config)
    await t.add_pull(await fake_context(repository, 3), config)
    await t.add_pull(await fake_context(repository, 4), config)
    await t.add_pull(await fake_context(repository, 5), config)

    await t.refresh()
    assert [[1], [1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5]] == get_cars_content(
        t
    )
    assert [] == get_waiting_content(t)

    await t.remove_pull(
        await fake_context(repository, 1, merged=True, merge_commit_sha="new_sha1")
    )

    with mock.patch.object(
        sys.modules[__name__],
        "MERGIFY_CONFIG",
        """
queue_rules:
  - name: five
    conditions: []
    speculative_checks: 2
""",
    ):
        repository._cache.mergify_config = context.NotCached
        await t.refresh()
    assert [[1, 2], [1, 2, 3]] == get_cars_content(t)
    assert [4, 5] == get_waiting_content(t)


@pytest.mark.asyncio
async def test_train_queue_config_change(
    repository,
    monkepatched_traincar,
):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("two", 1000))

    await t.refresh()
    assert [[1], [1, 2]] == get_cars_content(t)
    assert [3] == get_waiting_content(t)

    with mock.patch.object(
        sys.modules[__name__],
        "MERGIFY_CONFIG",
        """
queue_rules:
  - name: two
    conditions: []
    speculative_checks: 1
""",
    ):
        repository._cache.mergify_config = context.NotCached
        await t.refresh()
    assert [[1]] == get_cars_content(t)
    assert [2, 3] == get_waiting_content(t)


@pytest.mark.asyncio
@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_config_deleted(
    report_failure,
    repository,
    monkepatched_traincar,
):
    t = merge_train.Train(repository, "branch")
    await t.load()

    await t.add_pull(await fake_context(repository, 1), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 2), get_config("two", 1000))
    await t.add_pull(await fake_context(repository, 3), get_config("five", 1000))

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
        repository._cache.mergify_config = context.NotCached
        await t.refresh()
    assert [] == get_cars_content(t)
    assert [1, 2, 3] == get_waiting_content(t)
    assert len(report_failure.mock_calls) == 1


def test_train_batch_split():
    now = datetime.datetime.utcnow()
    t = merge_train.Train(repository, "branch")
    p1_two = merge_train.EmbarkedPull(1, get_config("two"), now)
    p2_two = merge_train.EmbarkedPull(2, get_config("two"), now)
    p3_two = merge_train.EmbarkedPull(3, get_config("two"), now)
    p4_five = merge_train.EmbarkedPull(4, get_config("five"), now)

    assert ([p1_two], [p2_two, p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "two", 1
    )
    assert ([p1_two, p2_two], [p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "two", 2
    )
    assert ([p1_two, p2_two, p3_two], [p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "two", 10
    )
    assert ([], [p1_two, p2_two, p3_two, p4_five]) == t._get_next_batch(
        [p1_two, p2_two, p3_two, p4_five], "five", 10
    )


@pytest.mark.asyncio
@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_2x5(
    report_failure,
    repository,
    monkepatched_traincar,
):
    t = merge_train.Train(repository, "branch")
    await t.load()

    for i in range(41, 46):
        await t.add_pull(await fake_context(repository, i), get_config("2x5", 1000))
    for i in range(6, 20):
        await t.add_pull(await fake_context(repository, i), get_config("2x5", 1000))

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

    # mark [41+42] as ready and merge it
    t._cars[0].checks_conclusion = check_api.Conclusion.SUCCESS
    await t.save()
    repository.installation.client.update_base_sha("sha41")
    await t.remove_pull(
        await fake_context(repository, 41, merged=True, merge_commit_sha="sha41")
    )
    repository.installation.client.update_base_sha("sha42")
    await t.remove_pull(
        await fake_context(repository, 42, merged=True, merge_commit_sha="sha42")
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

    # mark [43] as failure
    t._cars[0].checks_conclusion = check_api.Conclusion.FAILURE
    await t.save()
    await t.remove_pull(await fake_context(repository, 43, merged=False))

    # Train got cut after 43, and we restart from the begining
    await t.refresh()
    assert [
        [44, 45, 6, 7, 8],
        [44, 45, 6, 7, 8, 9, 10, 11, 12, 13],
    ] == get_cars_content(t)
    assert list(range(14, 20)) == get_waiting_content(t)
    assert len(t._cars[0].failure_history) == 0
    assert len(t._cars[1].failure_history) == 0


@pytest.mark.asyncio
@mock.patch("mergify_engine.queue.merge_train.TrainCar._set_creation_failure")
async def test_train_queue_splitted_on_failure_5x3(
    report_failure,
    repository,
    monkepatched_traincar,
):
    t = merge_train.Train(repository, "branch")
    await t.load()

    for i in range(41, 47):
        await t.add_pull(await fake_context(repository, i), get_config("5x3", 1000))
    for i in range(7, 22):
        await t.add_pull(await fake_context(repository, i), get_config("5x3", 1000))

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
    await t.remove_pull(await fake_context(repository, 41, merged=False))

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
    repository.installation.client.update_base_sha("sha42")
    await t.remove_pull(
        await fake_context(repository, 42, merged=True, merge_commit_sha="sha42")
    )
    repository.installation.client.update_base_sha("sha43")
    await t.remove_pull(
        await fake_context(repository, 43, merged=True, merge_commit_sha="sha43")
    )
    repository.installation.client.update_base_sha("sha44")
    await t.remove_pull(
        await fake_context(repository, 44, merged=True, merge_commit_sha="sha44")
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
    repository.installation.client.update_base_sha("sha45")
    await t.remove_pull(
        await fake_context(repository, 45, merged=True, merge_commit_sha="sha45")
    )
    repository.installation.client.update_base_sha("sha46")
    await t.remove_pull(
        await fake_context(repository, 46, merged=True, merge_commit_sha="sha46")
    )
    await t.refresh()
    assert [
        [42, 43, 44, 45, 46, 7],
    ] == get_cars_content(t)
    assert t._cars[0].checks_conclusion == check_api.Conclusion.FAILURE
    assert len(t._cars[0].failure_history) == 0

    # remove the failed 7
    await t.remove_pull(await fake_context(repository, 7, merged=False))

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
