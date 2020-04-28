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
from mergify_engine.actions.merge import action


PR = {
    "state": "unknown",
    "mergeable_state": "ok",
    "merged_by": "me",
    "merged": False,
    "merged_at": None,
    "title": "My PR title",
    "user": {"login": "jd"},
    "head": {"sha": "shasha"},
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
        ("Here's my message", "My PR title", "Here's my message", "title+body"),
    ],
)
def test_merge_commit_message(body, title, message, mode):
    pull = PR.copy()
    pull["body"] = body
    client = mock.Mock()
    ctxt = context.Context(client=client, pull=pull, subscription={})
    ctxt.checks = {"my CI": "success"}
    pr = ctxt.pull_request
    assert action.MergeAction._get_commit_message(pr, mode=mode) == (title, message)


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
def test_merge_commit_message_undefined(body):
    pull = PR.copy()
    pull["body"] = body
    pr = context.Context(client=mock.Mock(), pull=pull, subscription={}).pull_request
    with pytest.raises(context.RenderMessageFailure) as x:
        action.MergeAction._get_commit_message(pr)
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
def test_merge_commit_message_syntax_error(body, error):
    pull = PR.copy()
    pull["body"] = body
    pr = context.Context(client=mock.Mock(), pull=pull, subscription={}).pull_request
    with pytest.raises(context.RenderMessageFailure) as rmf:
        action.MergeAction._get_commit_message(pr)
        assert str(rmf) == error
