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


class GitHubInstallationAccessToken(typing.TypedDict):
    # https://developer.github.com/v3/apps/#response-7
    token: str
    expires_at: str


GitHubAccountType = typing.Literal["User", "Organization"]


class GitHubAccount(typing.TypedDict):
    login: str
    id: int
    type: GitHubAccountType


class GitHubInstallation(typing.TypedDict):
    # https://developer.github.com/v3/apps/#get-an-organization-installation-for-the-authenticated-app
    id: int
    account: GitHubAccount


GitHubRefType = typing.NewType("GitHubRefType", str)


class GitHubRepository(typing.TypedDict):
    id: int
    owner: GitHubAccount
    private: bool
    name: str
    full_name: str
    archived: bool
    url: str
    default_branch: GitHubRefType


class GitHubBranch(typing.TypedDict):
    label: str
    ref: GitHubRefType
    sha: str
    repo: GitHubRepository
    user: GitHubAccount


class GitHubLabel(typing.TypedDict):
    id: int
    name: str
    color: str
    default: bool


class GitHubComment(typing.TypedDict):
    id: int
    body: str
    user: GitHubAccount


class GitHubIssue(typing.TypedDict):
    number: int


GitHubPullRequestState = typing.Literal["open", "closed"]

# NOTE(sileht): Github mergeable_state is undocumented, here my finding by
# testing and and some info from other project:
#
# unknown: not yet computed by Github
# dirty: pull request conflict with the base branch
# behind: head branch is behind the base branch (only if strict: True)
# unstable: branch up2date (if strict: True) and not required status
#           checks are failure or pending
# clean: branch up2date (if strict: True) and all status check OK
# has_hooks: Mergeable with passing commit status and pre-recieve hooks.
#
# https://platform.github.community/t/documentation-about-mergeable-state/4259
# https://github.com/octokit/octokit.net/issues/1763
# https://developer.github.com/v4/enum/mergestatestatus/

GitHubPullRequestMergeableState = typing.Literal[
    "unknown",
    "dirty",
    "behind",
    "unstable",
    "clean",
    "has_hooks",
]


class GitHubPullRequest(GitHubIssue):
    # https://developer.github.com/v3/pulls/#get-a-pull-request
    id: int
    maintainer_can_modify: bool
    base: GitHubBranch
    head: GitHubBranch
    state: GitHubPullRequestState
    user: GitHubAccount
    labels: typing.List[GitHubLabel]
    merged: bool
    merged_by: typing.Optional[GitHubAccount]
    rebaseable: bool
    draft: bool
    merge_commit_sha: typing.Optional[str]
    mergeable_state: GitHubPullRequestMergeableState
    html_url: str


# https://docs.github.com/en/free-pro-team@latest/developers/webhooks-and-events/webhook-events-and-payloads
GitHubEventType = typing.Literal[
    "check_run",
    "check_suite",
    "pull_request",
    "push",
    # This does not exist in GitHub, it's a Mergify made one
    "refresh",
]


class GitHubEvent(typing.TypedDict):
    action: str
    repository: GitHubRepository
    organization: GitHubAccount
    installation: GitHubInstallation
    sender: GitHubAccount


class GitHubEventRefresh(GitHubEvent):
    ref: typing.Optional[GitHubRefType]
    pull_request: typing.Optional[GitHubPullRequest]


class GitHubEventPullRequest(GitHubEvent):
    pull_request: GitHubPullRequest


class GitHubEventPullRequestReviewComment(GitHubEvent):
    pull_request: GitHubPullRequest


class GitHubEventPullRequestReview(GitHubEvent):
    pull_request: GitHubPullRequest


class GitHubEventIssueComment(GitHubEvent):
    issue: GitHubIssue
    comment: GitHubComment


class GitHubEventPush(GitHubEvent):
    ref: GitHubRefType


class GitHubEventStatus(GitHubEvent):
    sha: str


class GitHubApp(typing.TypedDict):
    id: int


class GitHubCheckRun(typing.TypedDict):
    id: int
    app: GitHubApp
    external_id: str
    pull_requests: typing.List[GitHubPullRequest]
    head_sha: str


class GitHubCheckSuite(typing.TypedDict):
    id: int
    app: GitHubApp
    external_id: str
    pull_requests: typing.List[GitHubPullRequest]
    head_sha: str


class GitHubEventCheckRun(GitHubEvent):
    check_run: GitHubCheckRun


class GitHubEventCheckSuite(GitHubEvent):
    check_suite: GitHubCheckSuite
