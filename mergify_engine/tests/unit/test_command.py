# -*- encoding: utf-8 -*-
#
# Copyright © 2019 Mehdi Abaakouk <sileht@sileht.net>
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

from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import subscription
from mergify_engine.actions.backport import BackportAction
from mergify_engine.actions.rebase import RebaseAction
from mergify_engine.clients import github
from mergify_engine.engine import commands_runner


def test_command_loader():
    config = {"raw": {}}
    with pytest.raises(commands_runner.CommandInvalid):
        action = commands_runner.load_command(config, "@mergifyio notexist foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        action = commands_runner.load_command(config, "@mergifyio comment foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        action = commands_runner.load_command(config, "@Mergifyio comment foobar\n")

    for message in [
        "@mergify rebase",
        "@mergifyio rebase",
        "@Mergifyio rebase",
        "@mergifyio rebase\n",
        "@mergifyio rebase foobar",
        "@mergifyio rebase foobar\nsecondline\n",
    ]:
        command, args, action = commands_runner.load_command(config, message)
        assert command == "rebase"
        assert isinstance(action, RebaseAction)

    command, args, action = commands_runner.load_command(
        config, "@mergifyio backport branch-3.1 branch-3.2\nfoobar\n"
    )
    assert command == "backport"
    assert args == "branch-3.1 branch-3.2"
    assert isinstance(action, BackportAction)
    assert action.config == {
        "branches": ["branch-3.1", "branch-3.2"],
        "bot_account": None,
        "regexes": [],
        "ignore_conflicts": True,
        "labels": [],
        "label_conflicts": "conflicts",
        "assignees": [],
        "title": "{{ title }} (backport #{{ number }})",
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.io).\n{{ cherry_pick_error }}",
    }


def test_command_loader_with_defaults():
    config = {
        "raw": {
            "defaults": {
                "actions": {
                    "backport": {
                        "branches": ["branch-3.1", "branch-3.2"],
                        "ignore_conflicts": False,
                    }
                }
            }
        }
    }
    command = commands_runner.load_command(config, "@mergifyio backport")
    assert command.name == "backport"
    assert command.args == ""
    assert isinstance(command.action, BackportAction)
    assert command.action.config == {
        "assignees": [],
        "branches": ["branch-3.1", "branch-3.2"],
        "bot_account": None,
        "regexes": [],
        "ignore_conflicts": False,
        "labels": [],
        "label_conflicts": "conflicts",
        "title": "{{ title }} (backport #{{ number }})",
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.io).\n{{ cherry_pick_error }}",
    }


async def _create_context(redis_cache, client):
    sub = subscription.Subscription(
        redis_cache,
        123,
        "",
        {},
        frozenset({subscription.Features.PUBLIC_REPOSITORY}),
    )

    installation = context.Installation(123, "Mergifyio", sub, client, redis_cache)

    repository = context.Repository(
        installation, {"name": "demo", "id": 123, "private": True}
    )

    return await context.Context.create(
        repository,
        {
            "number": 789,
            "state": "open",
            "title": "Amazing new feature",
            "user": {
                "login": "octocat",
                "id": 1,
            },
            "mergeable_state": "ok",
            "merged_by": None,
            "merged": None,
            "merged_at": None,
            "base": {
                "sha": "sha",
                "ref": "main",
                "user": {
                    "login": {
                        "Mergifyio",
                    },
                },
                "repo": {
                    "name": "demo",
                    "private": False,
                    "owner": {
                        "login": "Mergifyio",
                        "id": 123,
                    },
                    "permissions": {
                        "admin": False,
                        "push": False,
                        "pull": True,
                    },
                },
            },
        },
        [],
    )


@pytest.mark.asyncio
async def test_run_command_without_rerun_and_without_user(redis_cache):

    client = mock.MagicMock()
    client.auth.installation.__getitem__.return_value = 123

    ctxt = await _create_context(redis_cache, client)

    with pytest.raises(RuntimeError) as error_msg:
        await commands_runner.handle(
            ctxt=ctxt, mergify_config={}, comment="@Mergifyio update", user=None
        )
    assert "user must be set if rerun is false" in str(error_msg.value)


@pytest.mark.asyncio
async def test_run_command_with_rerun_and_without_user(redis_cache, monkeypatch):

    client = github.aget_client(owner_id=123)

    ctxt = await _create_context(redis_cache, client)

    http_calls = []

    async def mock_post(*args, **kwargs):
        http_calls.append((args, kwargs))
        return

    monkeypatch.setattr(client, "post", mock_post)

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config={},
        comment="@mergifyio something",
        user=None,
        rerun=True,
    )

    assert (
        "Sorry but I didn't understand the command." in http_calls[0][1]["json"]["body"]
    )


@pytest.mark.parametrize(
    "user_id,permission, result",
    [
        (
            666,
            "nothing",
            "@wall-e is not allowed to run commands",
        ),
        (
            config.BOT_USER_ID,
            "nothing",
            "Sorry but I didn't understand the command",
        ),
        (
            1,
            "nothing",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "admin",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "maintain",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "write",
            "Sorry but I didn't understand the command",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_command_with_user(
    user_id, permission, result, redis_cache, monkeypatch
):
    client = github.aget_client(owner_id=123)

    ctxt = await _create_context(redis_cache, client)

    user = github_types.GitHubAccount(
        {
            "id": user_id,
            "login": "wall-e",
            "type": "Bot",
            "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
        },
    )

    class MockResponse:
        @staticmethod
        def json():
            return {
                "permission": permission,
                "user": {
                    "login": "wall-e",
                },
            }

    async def mock_get(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(client, "get", mock_get)

    http_calls = []

    async def mock_post(*args, **kwargs):
        http_calls.append((args, kwargs))
        return

    monkeypatch.setattr(client, "post", mock_post)

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config={},
        comment="unrelated",
        user=None,
        rerun=True,
    )
    assert len(http_calls) == 0

    await commands_runner.handle(
        ctxt=ctxt, mergify_config={}, comment="@mergifyio something", user=user
    )

    assert len(http_calls) == 1
    assert result in http_calls[0][1]["json"]["body"]


@pytest.mark.asyncio
async def test_run_command_with_wrong_arg(redis_cache, monkeypatch):
    client = github.aget_client(owner_id=123)

    ctxt = await _create_context(redis_cache, client)

    http_calls = []

    async def mock_post(*args, **kwargs):
        http_calls.append((args, kwargs))
        return

    monkeypatch.setattr(client, "post", mock_post)

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config={"raw": {}},
        comment="@mergifyio squash invalid-arg",
        rerun=True,
        user=None,
    )

    assert len(http_calls) == 1
    assert http_calls[0][1]["json"]["body"].startswith(
        "Sorry but I didn't understand the arguments of the command `squash`"
    )
