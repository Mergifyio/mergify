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

import pytest

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.tests.unit import conftest


@pytest.mark.parametrize(
    "body, title, message, template",
    [
        (
            """Hello world

# Commit Message
my title

my body""",
            "my title",
            "my body",
            None,
        ),
        (
            """Hello world

# Commit Message:
my title

my body
is longer""",
            "my title",
            "my body\nis longer",
            None,
        ),
        (
            """Hello world

# Commit Message
{{title}}

Authored-By: {{author}}
on two lines""",
            "My PR title",
            "Authored-By: contributor\non two lines",
            None,
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
            None,
        ),
        (
            """Hello world

# Commit Message

my title

my body""",
            "my title",
            "my body",
            None,
        ),
        (
            """Hello world

# Commit Message

my title     
WATCHOUT ^^^ there is empty spaces above for testing ^^^^
my body""",  # noqa:W293,W291
            "my title",
            "WATCHOUT ^^^ there is empty spaces above for testing ^^^^\nmy body",
            None,
        ),
        (
            # Should return an empty message
            """Hello world

# Commit Message

my title
""",
            "my title",
            "",
            None,
        ),
        (
            "",
            "My PR title (#43)",
            "",
            "{{title}} (#{{number}})\n\n{{body}}",
        ),
    ],
)
async def test_merge_commit_message(
    body: str,
    title: str,
    message: str,
    template: typing.Optional[str],
    context_getter: conftest.ContextGetterFixture,
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    ctxt.repository._caches.branch_protections[
        github_types.GitHubRefType("main")
    ] = None
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
    assert await ctxt.pull_request.get_commit_message(template=template) == (
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
async def test_merge_commit_message_undefined(
    body: str, context_getter: conftest.ContextGetterFixture
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    with pytest.raises(context.RenderTemplateFailure) as x:
        await ctxt.pull_request.get_commit_message()
    assert "foobar" in str(x.value)


@pytest.mark.parametrize(
    "body",
    [
        """Hello world

# Commit Message
{{title}}

here is my message {{ and broken template
"""
    ],
)
async def test_merge_commit_message_syntax_error(
    body: str, context_getter: conftest.ContextGetterFixture
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(43), body=body, title="My PR title"
    )
    with pytest.raises(context.RenderTemplateFailure):
        await ctxt.pull_request.get_commit_message()
