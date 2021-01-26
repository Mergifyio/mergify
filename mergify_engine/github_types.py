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


GitHubLogin = typing.NewType("GitHubLogin", str)


class GitHubInstallationAccessToken(typing.TypedDict):
    # https://developer.github.com/v3/apps/#response-7
    token: str
    expires_at: str


GitHubAccountType = typing.Literal["User", "Organization", "Bot"]
GitHubAccountIdType = typing.NewType("GitHubAccountIdType", int)


class GitHubAccount(typing.TypedDict):
    login: GitHubLogin
    id: GitHubAccountIdType
    type: GitHubAccountType


GitHubInstallationIdType = typing.NewType("GitHubInstallationIdType", int)


class GitHubInstallation(typing.TypedDict):
    # https://developer.github.com/v3/apps/#get-an-organization-installation-for-the-authenticated-app
    id: GitHubInstallationIdType
    account: GitHubAccount


GitHubRefType = typing.NewType("GitHubRefType", str)
SHAType = typing.NewType("SHAType", str)
GitHubRepositoryIdType = typing.NewType("GitHubRepositoryIdType", int)


GitHubRepositoryName = typing.NewType("GitHubRepositoryName", str)


class GitHubRepository(typing.TypedDict):
    id: GitHubRepositoryIdType
    owner: GitHubAccount
    private: bool
    name: GitHubRepositoryName
    full_name: str
    archived: bool
    url: str
    default_branch: GitHubRefType


GitHubTeamSlug = typing.NewType("GitHubTeamSlug", str)


class GitHubTeam(typing.TypedDict):
    slug: GitHubTeamSlug


class GitHubBranchCommitParent(typing.TypedDict):
    sha: SHAType


class GitHubBranchCommitCommit(typing.TypedDict):
    message: str


class GitHubBranchCommit(typing.TypedDict):
    sha: SHAType
    parents: typing.List[GitHubBranchCommitParent]
    commit: GitHubBranchCommitCommit


class GitHubBranch(typing.TypedDict):
    name: GitHubRefType
    commit: GitHubBranchCommit


class GitHubBranchRef(typing.TypedDict):
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


class GitHubFile(typing.TypedDict):
    filename: str
    contents_url: str


class GitHubIssueOrPullRequest(typing.TypedDict):
    pass


GitHubIssueId = typing.NewType("GitHubIssueId", int)
GitHubIssueNumber = typing.NewType("GitHubIssueNumber", int)


class GitHubIssue(GitHubIssueOrPullRequest):
    id: GitHubIssueId
    number: GitHubIssueNumber


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

GitHubPullRequestId = typing.NewType("GitHubPullRequestId", int)
GitHubPullRequestNumber = typing.NewType("GitHubPullRequestNumber", int)


ISODateTimeType = typing.NewType("ISODateTimeType", str)


class GitHubPullRequest(GitHubIssueOrPullRequest):
    # https://developer.github.com/v3/pulls/#get-a-pull-request
    id: GitHubPullRequestId
    number: GitHubPullRequestNumber
    maintainer_can_modify: bool
    base: GitHubBranchRef
    head: GitHubBranchRef
    state: GitHubPullRequestState
    user: GitHubAccount
    labels: typing.List[GitHubLabel]
    merged: bool
    merged_by: typing.Optional[GitHubAccount]
    merged_at: typing.Optional[ISODateTimeType]
    rebaseable: bool
    draft: bool
    merge_commit_sha: typing.Optional[SHAType]
    mergeable_state: GitHubPullRequestMergeableState
    html_url: str
    title: str


# https://docs.github.com/en/free-pro-team@latest/developers/webhooks-and-events/webhook-events-and-payloads
GitHubEventType = typing.Literal[
    "check_run",
    "check_suite",
    "pull_request",
    "status",
    "push",
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
    # This does not exist in GitHub, it's a Mergify made one
    "refresh",
]


class GitHubEvent(typing.TypedDict):
    organization: GitHubAccount
    installation: GitHubInstallation
    sender: GitHubAccount


GitHubEventRefreshActionType = typing.Literal[
    "user",
    "internal",
    "admin",
]


# This does not exist in GitHub, it's a Mergify made one
class GitHubEventRefresh(GitHubEvent):
    repository: GitHubRepository
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
    repository: GitHubRepository
    action: GitHubEventPullRequestActionType
    pull_request: GitHubPullRequest


GitHubEventPullRequestReviewCommentActionType = typing.Literal[
    "created",
    "edited",
    "deleted",
]


class GitHubEventPullRequestReviewComment(GitHubEvent):
    repository: GitHubRepository
    action: GitHubEventPullRequestReviewCommentActionType
    pull_request: GitHubPullRequest


GitHubEventPullRequestReviewActionType = typing.Literal[
    "submitted",
    "edited",
    "dismissed",
]


GitHubReviewIdType = typing.NewType("GitHubReviewIdType", int)
GitHubReviewStateType = typing.Literal[
    "APPROVED", "COMMENTED", "DISMISSED", "CHANGES_REQUESTED"
]


class GitHubReview(typing.TypedDict):
    id: GitHubReviewIdType
    user: GitHubAccount
    body: typing.Optional[str]
    pull_request: GitHubPullRequest
    repository: GitHubRepository
    state: GitHubReviewStateType


class GitHubEventPullRequestReview(GitHubEvent):
    repository: GitHubRepository
    action: GitHubEventPullRequestReviewActionType
    pull_request: GitHubPullRequest


GitHubEventIssueCommentActionType = typing.Literal[
    "created",
    "edited",
    "deleted",
]


class GitHubEventIssueComment(GitHubEvent):
    repository: GitHubRepository
    action: GitHubEventIssueCommentActionType
    issue: GitHubIssue
    comment: GitHubComment


class GitHubEventPush(GitHubEvent):
    repository: GitHubRepository
    ref: GitHubRefType
    before: SHAType
    after: SHAType


class GitHubEventStatus(GitHubEvent):
    repository: GitHubRepository
    sha: SHAType


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


GitHubStatusState = typing.Literal[
    "pending",
    "success",
    "failure",
    "error",
]


class GitHubStatus(typing.TypedDict):
    context: str
    state: GitHubStatusState


GitHubCheckRunStatus = typing.Literal["queued", "in_progress", "completed"]


class GitHubCheckRun(typing.TypedDict):
    id: int
    app: GitHubApp
    external_id: str
    pull_requests: typing.List[GitHubPullRequest]
    head_sha: SHAType
    before: SHAType
    after: SHAType
    name: str
    status: GitHubCheckRunStatus
    output: GitHubCheckRunOutput
    conclusion: typing.Optional[GitHubCheckRunConclusion]
    completed_at: ISODateTimeType


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
    repository: GitHubRepository
    action: GitHubCheckRunActionType
    app: GitHubApp
    check_run: GitHubCheckRun


GitHubCheckSuiteActionType = typing.Literal[
    "created",
    "completed",
    "rerequested",
    "requested_action",
]


class GitHubEventCheckSuite(GitHubEvent):
    repository: GitHubRepository
    action: GitHubCheckSuiteActionType
    app: GitHubApp
    check_suite: GitHubCheckSuite


GitHubEventOrganizationActionType = typing.Literal[
    "deleted",
    "renamed",
    "member_added",
    "member_removed",
    "member_invited",
]


class GitHubEventOrganization(GitHubEvent):
    action: GitHubEventOrganizationActionType


GitHubEventMemberActionType = typing.Literal["added", "removed", "edited"]


class GitHubEventMember(GitHubEvent):
    action: GitHubEventMemberActionType
    repository: GitHubRepository


GitHubEventMembershipActionType = typing.Literal["added", "removed"]


class GitHubEventMembership(GitHubEvent):
    action: GitHubEventMembershipActionType
    team: GitHubTeam


GitHubEventTeamActionType = typing.Literal[
    "created",
    "deleted",
    "edited",
    "added_to_repository",
    "removed_from_repository",
]


class GitHubEventTeam(GitHubEvent):
    action: GitHubEventTeamActionType
    repository: typing.Optional[GitHubRepository]
    team: GitHubTeam


class GitHubEventTeamAdd(GitHubEvent, total=False):
    # Repository key can be missing on Enterprise installations
    repository: GitHubRepository
