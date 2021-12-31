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
import typing
from unittest import mock

import pytest
import voluptuous

from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.actions import merge
from mergify_engine.actions import merge_base
from mergify_engine.rules import conditions
from mergify_engine.tests.unit import conftest


@pytest.mark.parametrize(
    "body, title, message, mode",
    [
        (
            """Hello world

# Commit Message
my title

my body""",
            "my title",
            "my body",
            "default",
        ),
        (
            """Hello world

# Commit Message:
my title

my body
is longer""",
            "my title",
            "my body\nis longer",
            "default",
        ),
        (
            """Hello world

# Commit Message
{{title}}

Authored-By: {{author}}
on two lines""",
            "My PR title",
            "Authored-By: contributor\non two lines",
            "default",
        ),
        (
            """Hello world
again!

## Commit Message
My Title

CI worked:
{% for ci in status_success %}
- {{ci}}
{% endfor %}
""",
            "My Title",
            "CI worked:\n\n- my CI\n",
            "default",
        ),
        (
            """Hello world

# Commit Message

my title

my body""",
            "my title",
            "my body",
            "default",
        ),
        (
            """Hello world

# Commit Message

my title     
WATCHOUT ^^^ there is empty spaces above for testing ^^^^
my body""",  # noqa:W293,W291
            "my title",
            "WATCHOUT ^^^ there is empty spaces above for testing ^^^^\nmy body",
            "default",
        ),
        (
            # Should return an empty message
            """Hello world

# Commit Message

my title
""",
            "my title",
            "",
            "default",
        ),
        ("Here's my message", "My PR title (#43)", "Here's my message", "title+body"),
    ],
)
@pytest.mark.asyncio
async def test_merge_commit_message(body, title, message, mode, context_getter):
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    ctxt.repository._caches.branch_protections["main"] = None
    ctxt._caches.pull_statuses.set(
        [
            github_types.GitHubStatus(
                {
                    "target_url": "http://example.com",
                    "context": "my CI",
                    "state": "success",
                    "description": "foobar",
                    "avatar_url": "",
                }
            )
        ]
    )
    ctxt._caches.pull_check_runs.set([])
    assert await ctxt.pull_request.get_commit_message(mode=mode) == (
        title,
        message,
    )


@pytest.mark.parametrize(
    "body",
    [
        (
            """Hello world

# Commit Message
{{title}}

here is my message {{foobar}}
on two lines"""
        ),
        (
            """Hello world

# Commit Message
{{foobar}}

here is my message
on two lines"""
        ),
    ],
)
@pytest.mark.asyncio
async def test_merge_commit_message_undefined(
    body: str, context_getter: conftest.ContextGetterFixture
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    with pytest.raises(context.RenderTemplateFailure) as x:
        await ctxt.pull_request.get_commit_message()
        assert str(x) == "foobar"


@pytest.mark.parametrize(
    "body,error",
    [
        (
            """Hello world

# Commit Message
{{title}}

here is my message {{ and broken template
""",
            "lol",
        ),
    ],
)
@pytest.mark.asyncio
async def test_merge_commit_message_syntax_error(
    body: str, error: str, context_getter: conftest.ContextGetterFixture
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    with pytest.raises(context.RenderTemplateFailure) as rmf:
        await ctxt.pull_request.get_commit_message()
        assert str(rmf) == error


def gen_config(priorities: typing.List[int]) -> typing.List[typing.Dict[str, int]]:
    return [{"priority": priority} for priority in priorities]


@pytest.mark.asyncio
async def test_queue_summary(context_getter: conftest.ContextGetterFixture) -> None:
    ctxt = await context_getter(github_types.GitHubPullRequestNumber(0))
    ctxt.repository.get_pull_request_context = mock.AsyncMock(  # type: ignore[assignment]
        return_value=mock.Mock(pull={"title": "foo"})
    )
    q = mock.AsyncMock(installation_id=12345)
    q.get_pulls.return_value = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    q.get_config.side_effect = gen_config(
        [4000, 3000, 3000, 3000, 2000, 2000, 1000, 1000, 1000]
    )
    with mock.patch.object(merge.naive.Queue, "from_context", return_value=q):
        action = merge.MergeAction(
            voluptuous.Schema(merge.MergeAction.get_schema())({}), {}
        )
        assert """**Required conditions for merge:**


**The following pull requests are queued:**
| | Pull request | Priority |
| ---: | :--- | :--- |
| 1 | foo #1 | 4000 |
| 2 | foo #2 | high |
| 3 | foo #3 | high |
| 4 | foo #4 | high |
| 5 | foo #5 | medium |
| 6 | foo #6 | medium |
| 7 | foo #7 | low |
| 8 | foo #8 | low |
| 9 | foo #9 | low |

---

""" + constants.MERGIFY_PULL_REQUEST_DOC == await action._get_queue_summary(
            ctxt, mock.Mock(conditions=conditions.QueueRuleConditions([])), q
        )


@pytest.mark.parametrize(
    "test_input, expected",
    [
        (True, merge_base.StrictMergeParameter.true),
        (False, merge_base.StrictMergeParameter.false),
        ("smart", merge_base.StrictMergeParameter.ordered),
        ("smart+ordered", merge_base.StrictMergeParameter.ordered),
        ("smart+fasttrack", merge_base.StrictMergeParameter.fasttrack),
        ("smart+fastpath", merge_base.StrictMergeParameter.fasttrack),
    ],
)
def test_strict_merge_parameter_ok(test_input, expected):
    assert merge_base.strict_merge_parameter(test_input) == expected


def test_strict_merge_parameter_fail():
    with pytest.raises(
        ValueError,
        match="toto is an unknown strict merge parameter",
    ):
        merge_base.strict_merge_parameter("toto")
