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
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.actions.backport import BackportAction
from mergify_engine.actions.rebase import RebaseAction
from mergify_engine.clients import github
from mergify_engine.dashboard import subscription
from mergify_engine.engine import commands_runner


EMPTY_CONFIG = rules.get_mergify_config(
    context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha=github_types.SHAType("azertyuiop"),
        path="whatever",
        decoded_content=b"",
    )
)


def test_command_loader() -> None:
    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@mergifyio notexist foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@mergifyio comment foobar\n")

    with pytest.raises(commands_runner.CommandInvalid):
        commands_runner.load_command(EMPTY_CONFIG, "@Mergifyio comment foobar\n")

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
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.io).\n{{ cherry_pick_error }}",
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
        decoded_content=raw_config.encode(),
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
        "body": "This is an automatic backport of pull request #{{number}} done by [Mergify](https://mergify.io).\n{{ cherry_pick_error }}",
    }


async def _create_context(
    redis_cache: utils.RedisCache, client: github.AsyncGithubInstallationClient
) -> context.Context:
    sub = subscription.Subscription(
        redis_cache,
        123,
        "",
        frozenset({subscription.Features.PUBLIC_REPOSITORY}),
        0,
    )
    gh_user = github_types.GitHubAccount(
        {
            "type": "User",
            "id": github_types.GitHubAccountIdType(1),
            "login": github_types.GitHubLogin("octocat"),
            "avatar_url": "",
        }
    )

    gh_owner = github_types.GitHubAccount(
        {
            "type": "User",
            "id": github_types.GitHubAccountIdType(123),
            "login": github_types.GitHubLogin("Mergifyio"),
            "avatar_url": "",
        }
    )
    gh_repo = github_types.GitHubRepository(
        {
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("main"),
            "id": github_types.GitHubRepositoryIdType(123),
            "full_name": "user/ref",
            "name": github_types.GitHubRepositoryName("demo"),
            "private": False,
            "owner": gh_owner,
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
        sub,
        client,
        redis_cache,
    )

    repository = context.Repository(installation, gh_repo)

    return await context.Context.create(
        repository,
        github_types.GitHubPullRequest(
            {
                "node_id": "42",
                "locked": False,
                "assignees": [],
                "requested_reviewers": [],
                "requested_teams": [],
                "milestone": None,
                "title": "",
                "body": "",
                "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "closed_at": None,
                "id": github_types.GitHubPullRequestId(0),
                "maintainer_can_modify": False,
                "rebaseable": False,
                "draft": False,
                "merge_commit_sha": None,
                "labels": [],
                "number": github_types.GitHubPullRequestNumber(789),
                "commits": 1,
                "state": "open",
                "html_url": "<html_url>",
                "user": gh_user,
                "merged": False,
                "head": {
                    "label": "Mergifyio:feature",
                    "sha": github_types.SHAType("sha2"),
                    "ref": github_types.GitHubRefType("feature"),
                    "user": gh_owner,
                    "repo": gh_repo,
                },
                "base": {
                    "label": "Mergifyio:main",
                    "sha": github_types.SHAType("sha1"),
                    "ref": github_types.GitHubRefType("main"),
                    "user": gh_owner,
                    "repo": gh_repo,
                },
                "merged_by": None,
                "merged_at": None,
                "mergeable_state": "clean",
                "changed_files": 300,
            }
        ),
        [],
    )


@pytest.mark.asyncio
async def test_run_command_without_rerun_and_without_user(
    redis_cache: utils.RedisCache,
) -> None:

    client = mock.MagicMock()
    client.auth.installation.__getitem__.return_value = 123

    ctxt = await _create_context(redis_cache, client)

    with pytest.raises(RuntimeError) as error_msg:
        await commands_runner.handle(
            ctxt=ctxt,
            mergify_config=EMPTY_CONFIG,
            comment="@Mergifyio update",
            user=None,
        )
    assert "user must be set if rerun is false" in str(error_msg.value)


@pytest.mark.asyncio
async def test_run_command_with_rerun_and_without_user(
    redis_cache: utils.RedisCache,
) -> None:

    client = mock.Mock()
    client.post = mock.AsyncMock()

    ctxt = await _create_context(redis_cache, client)

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
    user_id: int, permission: str, result: str, redis_cache: utils.RedisCache
) -> None:

    client = mock.Mock()

    ctxt = await _create_context(redis_cache, client)

    user = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(user_id),
            "login": github_types.GitHubLogin("wall-e"),
            "type": "Bot",
            "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4",
        },
    )

    client.item = mock.AsyncMock()
    client.item.return_value = {
        "permission": permission,
        "user": user,
    }
    client.post = mock.AsyncMock()

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
        comment="@mergifyio something",
        user=user,
    )

    assert len(client.post.call_args_list) == 1
    assert result in client.post.call_args_list[0][1]["json"]["body"]


@pytest.mark.asyncio
async def test_run_command_with_wrong_arg(redis_cache: utils.RedisCache) -> None:

    client = mock.Mock()
    client.post = mock.AsyncMock()

    ctxt = await _create_context(redis_cache, client)

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
