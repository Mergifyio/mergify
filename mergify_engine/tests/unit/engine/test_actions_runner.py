# -*- encoding: utf-8 -*-
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

import pytest

from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.engine import actions_runner
from mergify_engine.tests.unit import conftest


@pytest.mark.parametrize(
    "merged_by,raw_config,result",
    [
        (
            config.BOT_USER_LOGIN,
            """
pull_request_rules:
 - name: Automatic merge on approval
   conditions:
     - and:
       - "-draft"
       - "author=contributor"
   actions:
     merge:
       method: merge
""",
            "",
        ),
        (
            "foobar",
            """
queue_rules:
  - name: foo
    conditions: []
pull_request_rules:
 - name: Automatic queue on approval
   conditions:
     - and:
       - "-draft"
       - "author=contributor"
   actions:
     queue:
       name: foo
        """,
            "⚠️ The pull request has been merged by @foobar\n\n",
        ),
        (
            config.BOT_USER_LOGIN,
            """
pull_request_rules:
 - name: Automatic queue on approval
   conditions:
     - and:
       - "-draft"
       - "author=contributor"
   actions:
     delete_head_branch:
        """,
            "⚠️ The pull request has been closed by GitHub because its commits are also part of another pull request\n\n",
        ),
    ],
)
async def test_get_already_merged_summary(
    merged_by: str,
    raw_config: str,
    result: str,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    ctxt = await context_getter(
        github_types.GitHubPullRequestNumber(1),
        merged=True,
        merged_by=github_types.GitHubAccount(
            {
                "id": github_types.GitHubAccountIdType(1),
                "login": github_types.GitHubLogin(merged_by),
                "type": "User",
                "avatar_url": "",
            }
        ),
    )
    ctxt.repository._caches.branch_protections[
        github_types.GitHubRefType("main")
    ] = None

    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha=github_types.SHAType("azertyuiop"),
        path="whatever",
        decoded_content=raw_config,
    )

    config = rules.get_mergify_config(file)
    match = await config["pull_request_rules"].get_pull_request_rule(ctxt)
    assert result == await actions_runner.get_already_merged_summary(ctxt, match)
