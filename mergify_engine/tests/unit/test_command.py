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
import typing
from unittest import mock

import pytest

from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.actions.backport import BackportAction
from mergify_engine.actions.rebase import RebaseAction
from mergify_engine.engine import commands_runner
from mergify_engine.tests.unit import conftest


EMPTY_CONFIG = rules.get_mergify_config(
    context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha=github_types.SHAType("azertyuiop"),
        path="whatever",
        decoded_content="",
    )
)


def test_command_loader() -> None:
    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@mergifyio notexist foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@mergifyio comment foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@Mergifyio comment foobar\n")

    with pytest.raises(commands_runner.NotACommand):
        commands_runner.load_command(EMPTY_CONFIG, "comment @Mergifyio test foobar\n")

    for message in [
        "@mergify rebase",
        "@mergifyio rebase",
        "@Mergifyio rebase",
        "@mergifyio rebase\n",
        "@mergifyio rebase foobar",
        "@mergifyio rebase foobar\nsecondline\n",
    ]:
        command, args, action = commands_runner.load_command(EMPTY_CONFIG, message)
        assert command == "rebase"
        assert isinstance(action, RebaseAction)

    command, args, action = commands_runner.load_command(
        EMPTY_CONFIG, "@mergifyio backport branch-3.1 branch-3.2\nfoobar\n"
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
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.com).\n{{ cherry_pick_error }}",
    }


def test_command_loader_with_defaults() -> None:
    raw_config = """
defaults:
  actions:
    backport:
      branches:
        - branch-3.1
        - branch-3.2
      ignore_conflicts: false
"""

    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha=github_types.SHAType("azertyuiop"),
        path="whatever",
        decoded_content=raw_config,
    )
    config = rules.get_mergify_config(file)
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
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.com).\n{{ cherry_pick_error }}",
    }


async def test_run_command_without_rerun_and_without_user(
    context_getter: conftest.ContextGetterFixture,
) -> None:

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))

    with pytest.raises(RuntimeError) as error_msg:
        await commands_runner.handle(
            ctxt=ctxt,
            mergify_config=EMPTY_CONFIG,
            comment="@Mergifyio update",
            user=None,
        )
    assert "user must be set if rerun is false" in str(error_msg.value)


async def test_run_command_with_rerun_and_without_user(
    context_getter: conftest.ContextGetterFixture,
) -> None:

    client = mock.Mock()
    client.post = mock.AsyncMock()

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config=EMPTY_CONFIG,
        comment="@mergifyio something",
        user=None,
        rerun=True,
    )
    assert len(client.post.call_args_list) == 1
    assert (
        "Sorry but I didn't understand the command."
        in client.post.call_args_list[0][1]["json"]["body"]
    )


@pytest.mark.parametrize(
    "user_id,permission,comment,result",
    [
        (
            666,
            "nothing",
            "not a command",
            None,
        ),
        (
            666,
            "nothing",
            "@mergifyio squash",
            "@wall-e is not allowed to run commands",
        ),
        (
            config.BOT_USER_ID,
            "nothing",
            "@mergifyio something",
            "Sorry but I didn't understand the command",
        ),
        (
            123,
            "nothing",
            "@mergifyio something",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "admin",
            "@mergifyio squash",
            "Pull request is already one-commit long",
        ),
        (
            666,
            "admin",
            f"{config.GITHUB_URL}/Mergifyio squash",
            "Pull request is already one-commit long",
        ),
        (
            666,
            "admin",
            "@mergifyio something",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "maintain",
            "@mergifyio something",
            "Sorry but I didn't understand the command",
        ),
        (
            666,
            "write",
            "@mergifyio something",
            "Sorry but I didn't understand the command",
        ),
    ],
)
async def test_run_command_with_user(
    user_id: int,
    permission: str,
    comment: str,
    result: typing.Optional[str],
    context_getter: conftest.ContextGetterFixture,
) -> None:

    user = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(user_id),
            "login": github_types.GitHubLogin("wall-e"),
            "type": "Bot",
            "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
        },
    )

    client = mock.Mock()
    client.item = mock.AsyncMock()
    client.item.return_value = {
        "permission": permission,
        "user": user,
    }
    client.post = mock.AsyncMock()

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config=EMPTY_CONFIG,
        comment="unrelated",
        user=None,
        rerun=True,
    )
    assert len(client.post.call_args_list) == 0

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config=EMPTY_CONFIG,
        comment=comment,
        user=user,
    )

    if result is None:
        assert len(client.post.call_args_list) == 0
    else:
        assert len(client.post.call_args_list) == 1
        assert result in client.post.call_args_list[0][1]["json"]["body"]


async def test_run_command_with_wrong_arg(
    context_getter: conftest.ContextGetterFixture,
) -> None:

    client = mock.Mock()
    client.post = mock.AsyncMock()

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client

    await commands_runner.handle(
        ctxt=ctxt,
        mergify_config=EMPTY_CONFIG,
        comment="@mergifyio squash invalid-arg",
        rerun=True,
        user=None,
    )

    assert len(client.post.call_args_list) == 1
    assert client.post.call_args_list[0][1]["json"]["body"].startswith(
        "Sorry but I didn't understand the arguments of the command `squash`"
    )
