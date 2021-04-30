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
import voluptuous

from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine.actions import merge
from mergify_engine.actions import merge_base


PR = {
    "number": 43,
    "state": "unknown",
    "mergeable_state": "ok",
    "merged_by": {"login": "me"},
    "merged": False,
    "merged_at": None,
    "title": "My PR title",
    "user": {"login": "jd"},
    "head": {"ref": "fork", "sha": "shasha"},
    "base": {
        "ref": "master",
        "user": {"login": "jd"},
        "repo": {"name": "repo", "private": False},
        "sha": "miaou",
    },
    "assignees": [],
    "locked": False,
    "labels": [],
    "requested_reviewers": [],
    "requested_teams": [],
    "milestone": None,
}


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
            "Authored-By: jd\non two lines",
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
async def test_merge_commit_message(body, title, message, mode):
    pull = PR.copy()
    pull["body"] = body
    client = mock.MagicMock()
    installation = context.Installation(123, "whatever", {}, client, None)
    repository = context.Repository(installation, "whatever", 123)
    repository._cache["branches"] = {"master": {"protection": {"enabled": False}}}
    ctxt = await context.Context.create(repository=repository, pull=pull)
    ctxt._cache["pull_statuses"] = [
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
    ctxt._cache["pull_check_runs"] = []
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
async def test_merge_commit_message_undefined(body):
    pull = PR.copy()
    pull["body"] = body
    client = mock.MagicMock()
    installation = context.Installation(123, "whatever", {}, client, None)
    repository = context.Repository(installation, "whatever", 123)
    pr = await context.Context.create(repository=repository, pull=pull)
    with pytest.raises(context.RenderTemplateFailure) as x:
        await pr.pull_request.get_commit_message()
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
async def test_merge_commit_message_syntax_error(body, error, redis_cache):
    pull = PR.copy()
    pull["body"] = body
    client = mock.MagicMock()
    installation = context.Installation(123, "whatever", {}, client, redis_cache)
    repository = context.Repository(installation, "whatever", 123)
    pr = await context.Context.create(repository=repository, pull=pull)
    with pytest.raises(context.RenderTemplateFailure) as rmf:
        await pr.pull_request.get_commit_message()
        assert str(rmf) == error


def gen_config(priorities):
    return [{"priority": priority} for priority in priorities]


@pytest.mark.asyncio
async def test_queue_summary(redis_cache):
    repository = mock.Mock(
        get_pull_request_context=mock.AsyncMock(
            return_value=mock.Mock(pull={"title": "foo"})
        )
    )
    ctxt = mock.Mock(
        repository=repository,
        subscription=subscription.Subscription(
            redis_cache,
            123,
            True,
            "We're just testing",
            frozenset({subscription.Features.PRIORITY_QUEUES}),
        ),
    )
    ctxt.missing_feature_reason = subscription.Subscription.missing_feature_reason
    ctxt.pull = {
        "base": {
            "repo": {
                "owner": {
                    "login": "Mergifyio",
                },
            },
        },
    }
    q = mock.AsyncMock(installation_id=12345)
    q.get_pulls.return_value = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    q.get_config.side_effect = gen_config(
        [4000, 3000, 3000, 3000, 2000, 2000, 1000, 1000, 1000]
    )
    with mock.patch.object(merge.naive.Queue, "from_context", return_value=q):
        action = merge.MergeAction(voluptuous.Schema(merge.MergeAction.validator)({}))
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
            ctxt, mock.Mock(conditions=rules.RuleConditions([])), q
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
