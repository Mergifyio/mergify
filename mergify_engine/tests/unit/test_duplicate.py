# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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

from mergify_engine import duplicate_pull
from mergify_engine import github_types
from mergify_engine.tests.unit import conftest


async def fake_get_github_pulls_from_sha(
    url, resource_name, page_limit, api_version=None
):
    pr = {
        "number": 6,
        "base": {
            "ref": "main",
            "sha": "the-base-sha",
            "repo": {
                "full_name": "Mergifyio/mergify-engine",
                "name": "mergify-engine",
                "private": False,
            },
        },
        "head": {
            "ref": "main",
            "repo": {
                "full_name": "contributor/mergify-engine",
                "name": "mergify-engine",
                "private": False,
            },
        },
    }
    if url.endswith("commits/rebased_c1/pulls"):
        yield pr
    elif url.endswith("commits/rebased_c2/pulls"):
        yield pr
    else:
        return


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
async def test_get_commits_to_cherry_pick_rebase(
    commits: mock.PropertyMock,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    c1 = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("c1f"),
            "parents": [],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )
    c2 = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("c2"),
            "parents": [c1["sha"]],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )
    commits.return_value = [c1, c2]

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"
    client.items.side_effect = fake_get_github_pulls_from_sha

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(6))
    ctxt.repository.installation.client = client

    base_branch = github_types.GitHubBranchCommitParent(
        {"sha": github_types.SHAType("base_branch")}
    )
    rebased_c1 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("rebased_c1"),
            "parents": [base_branch],
            "commit": {"message": "hello c1", "verification": {"verified": False}},
        }
    )
    rebased_c2 = github_types.GitHubBranchCommit(
        {
            "sha": github_types.SHAType("rebased_c2"),
            "parents": [rebased_c1],
            "commit": {"message": "hello c2", "verification": {"verified": False}},
        }
    )

    async def fake_get_github_commit_from_sha(url, api_version=None):
        if url.endswith("/commits/rebased_c1"):
            return rebased_c1
        if url.endswith("/commits/rebased_c2"):
            return rebased_c2
        raise RuntimeError(f"Unknown URL {url}")

    client.item.side_effect = fake_get_github_commit_from_sha

    assert await duplicate_pull._get_commits_to_cherrypick(
        ctxt, github_types.to_cached_github_branch_commit(rebased_c2)
    ) == [
        github_types.to_cached_github_branch_commit(rebased_c1),
        github_types.to_cached_github_branch_commit(rebased_c2),
    ]


@mock.patch(
    "mergify_engine.context.Context.commits",
    new_callable=mock.PropertyMock,
)
async def test_get_commits_to_cherry_pick_merge(
    commits: mock.PropertyMock,
    context_getter: conftest.ContextGetterFixture,
) -> None:
    c1 = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("c1f"),
            "parents": [],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )
    c2 = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("c2"),
            "parents": [c1["sha"]],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )

    async def fake_commits() -> typing.List[github_types.CachedGitHubBranchCommit]:
        return [c1, c2]

    commits.return_value = fake_commits()

    client = mock.Mock()
    client.auth.get_access_token.return_value = "<token>"

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(1))
    ctxt.repository.installation.client = client

    base_branch = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("base_branch"),
            "parents": [],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )
    merge_commit = github_types.CachedGitHubBranchCommit(
        {
            "sha": github_types.SHAType("merge_commit"),
            "parents": [base_branch["sha"], c2["sha"]],
            "commit_message": "foobar",
            "commit_verification_verified": False,
        }
    )

    assert await duplicate_pull._get_commits_to_cherrypick(ctxt, merge_commit) == [
        c1,
        c2,
    ]
