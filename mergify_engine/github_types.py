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
SHAType = typing.NewType("SHAType", str)


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
    sha: SHAType
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
    merge_commit_sha: typing.Optional[SHAType]
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
    repository: GitHubRepository
    organization: GitHubAccount
    installation: GitHubInstallation
    sender: GitHubAccount


GitHubEventRefreshActionType = typing.Literal[
    "user",
    "forced",
]


# This does not exist in GitHub, it's a Mergify made one
class GitHubEventRefresh(GitHubEvent):
    action: GitHubEventRefreshActionType
    ref: typing.Optional[GitHubRefType]
    pull_request: typing.Optional[GitHubPullRequest]


GitHubEventPullRequestActionType = typing.Literal[
    "opened",
    "edited",
    "closed",
    "assigned",
    "unassigned",
    "review_requested",
    "review_request_removed",
    "ready_for_review",
    "labeled",
    "unlabeled",
    "synchronize",
    "locked",
    "unlocked",
    "reopened",
]


class GitHubEventPullRequest(GitHubEvent):
    action: GitHubEventPullRequestActionType
    pull_request: GitHubPullRequest


GitHubEventPullRequestReviewCommentActionType = typing.Literal[
    "created",
    "edited",
    "deleted",
]


class GitHubEventPullRequestReviewComment(GitHubEvent):
    action: GitHubEventPullRequestReviewCommentActionType
    pull_request: GitHubPullRequest


GitHubEventPullRequestReviewActionType = typing.Literal[
    "submitted",
    "edited",
    "dismissed",
]


class GitHubEventPullRequestReview(GitHubEvent):
    action: GitHubEventPullRequestReviewActionType
    pull_request: GitHubPullRequest


GitHubEventIssueCommentActionType = typing.Literal[
    "created",
    "edited",
    "deleted",
]


class GitHubEventIssueComment(GitHubEvent):
    action: GitHubEventIssueCommentActionType
    issue: GitHubIssue
    comment: GitHubComment


class GitHubEventPush(GitHubEvent):
    ref: GitHubRefType
    before: SHAType
    after: SHAType


class GitHubEventStatus(GitHubEvent):
    sha: str


class GitHubApp(typing.TypedDict):
    id: int


GitHubCheckRunConclusion = typing.Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
]


class GitHubCheckRunOutput(typing.TypedDict):
    title: typing.Optional[str]
    summary: typing.Optional[str]
    text: typing.Optional[str]


class GitHubCheckRun(typing.TypedDict):
    id: int
    app: GitHubApp
    external_id: str
    pull_requests: typing.List[GitHubPullRequest]
    head_sha: SHAType
    before: SHAType
    after: SHAType
    name: str
    output: GitHubCheckRunOutput
    conclusion: typing.Optional[GitHubCheckRunConclusion]


class GitHubCheckSuite(typing.TypedDict):
    id: int
    app: GitHubApp
    external_id: str
    pull_requests: typing.List[GitHubPullRequest]
    head_sha: SHAType
    before: SHAType
    after: SHAType


GitHubCheckRunActionType = typing.Literal[
    "created",
    "completed",
    "rerequested",
    "requested_action",
]


class GitHubEventCheckRun(GitHubEvent):
    action: GitHubCheckRunActionType
    check_run: GitHubCheckRun


GitHubCheckSuiteActionType = typing.Literal[
    "created",
    "completed",
    "rerequested",
    "requested_action",
]


class GitHubEventCheckSuite(GitHubEvent):
    action: GitHubCheckSuiteActionType
    check_suite: GitHubCheckSuite
