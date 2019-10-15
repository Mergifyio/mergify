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

import collections
import itertools
import re
from urllib import parse

import attr

import daiquiri

import github

import tenacity

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import exceptions

LOG = daiquiri.getLogger(__name__)

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?$", re.I)


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

GenericCheck = collections.namedtuple("GenericCheck", ["context", "state"])


@attr.s()
class MergifyPull(object):
    # NOTE(sileht): Use from_cache/from_event not the constructor directly
    g = attr.ib()
    g_pull = attr.ib()
    installation_id = attr.ib()
    _consolidated_data = attr.ib(init=False, default=None)

    @classmethod
    def from_raw(cls, installation_id, installation_token, pull_raw):
        g = github.Github(
            installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        pull = github.PullRequest.PullRequest(
            g._Github__requester, {}, pull_raw, completed=True
        )
        return cls(g, pull, installation_id)

    @classmethod
    def from_number(
        cls, installation_id, installation_token, owner, reponame, pull_number
    ):
        g = github.Github(
            installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        repo = g.get_repo(owner + "/" + reponame)
        pull = repo.get_pull(pull_number)
        return cls(g, pull, installation_id)

    def __attrs_post_init__(self):
        self._ensure_mergable_state()

    def _valid_perm(self, login):
        return self.g_pull.base.repo.get_collaborator_permission(login) in [
            "admin",
            "write",
        ]

    def _get_reviews(self):
        # Ignore reviews that are not from someone with admin/write permissions
        # And only keep the last review for each user.
        reviews = list(self.g_pull.get_reviews())
        valid_users = list(
            filter(self._valid_perm, set([r.user.login for r in reviews]))
        )
        comments = dict()
        approvals = dict()
        for review in reviews:
            if review.user.login not in valid_users:
                continue
            # Only keep latest review of an user
            if review.state == "COMMENTED":
                comments[review.user.login] = review
            else:
                approvals[review.user.login] = review
        return list(comments.values()), list(approvals.values())

    def to_dict(self):
        if self._consolidated_data is None:
            self._consolidated_data = self._get_consolidated_data()
        return self._consolidated_data

    def _get_consolidated_data(self):
        comments, approvals = self._get_reviews()
        statuses = self._get_checks()
        # FIXME(jd) pygithub does 2 HTTP requests whereas 1 is enough!
        review_requested_users, review_requested_teams = (
            self.g_pull.get_review_requests()
        )
        return {
            # Only use internally attributes
            "_approvals": approvals,
            # Can be used by rules too
            "assignee": [a.login for a in self.g_pull.assignees],
            # NOTE(sileht): We put an empty label to allow people to match
            # no label set
            "label": [l.name for l in self.g_pull.labels],
            "review-requested": (
                [u.login for u in review_requested_users]
                + ["@" + t.slug for t in review_requested_teams]
            ),
            "author": self.g_pull.user.login,
            "merged-by": (self.g_pull.merged_by.login if self.g_pull.merged_by else ""),
            "merged": self.g_pull.merged,
            "closed": self.g_pull.state == "closed",
            "milestone": (self.g_pull.milestone.title if self.g_pull.milestone else ""),
            "conflict": self.g_pull.mergeable_state == "dirty",
            "base": self.g_pull.base.ref,
            "head": self.g_pull.head.ref,
            "locked": self.g_pull._rawData["locked"],
            "title": self.g_pull.title,
            "body": self.g_pull.body,
            "files": [f.filename for f in self.g_pull.get_files()],
            "approved-reviews-by": [
                r.user.login for r in approvals if r.state == "APPROVED"
            ],
            "dismissed-reviews-by": [
                r.user.login for r in approvals if r.state == "DISMISSED"
            ],
            "changes-requested-reviews-by": [
                r.user.login for r in approvals if r.state == "CHANGES_REQUESTED"
            ],
            "commented-reviews-by": [
                r.user.login for r in comments if r.state == "COMMENTED"
            ],
            "status-success": [s.context for s in statuses if s.state == "success"],
            # NOTE(jd) The Check API set conclusion to None for pending.
            # NOTE(sileht): "pending" statuses are not really trackable, we
            # voluntary drop this event because CIs just sent they status every
            # minutes until the CI pass (at least Travis and Circle CI does
            # that). This was causing a big load on Mergify for nothing useful
            # tracked, and on big projects it can reach the rate limit very
            # quickly.
            # "status-pending": [s.context for s in statuses
            #                    if s.state in ("pending", None)],
            "status-failure": [s.context for s in statuses if s.state == "failure"],
            "status-neutral": [s.context for s in statuses if s.state == "neutral"],
            # NOTE(sileht): Not handled for now
            # cancelled, timed_out, or action_required
        }

    def _get_statuses(self):
        already_seen = set()
        statuses = []
        for status in github.PaginatedList.PaginatedList(
            github.CommitStatus.CommitStatus,
            self.g_pull._requester,
            self.g_pull.base.repo.url
            + "/commits/"
            + self.g_pull.head.sha
            + "/statuses",
            None,
        ):
            if status.context not in already_seen:
                already_seen.add(status.context)
                statuses.append(status)
        return statuses

    def _get_checks(self):
        generic_checks = set()
        try:
            # NOTE(sileht): conclusion can be one of success, failure, neutral,
            # cancelled, timed_out, or action_required, and  None for "pending"
            generic_checks |= set(
                [
                    GenericCheck(c.name, c.conclusion)
                    for c in check_api.get_checks(self.g_pull)
                ]
            )
        except github.GithubException as e:
            if (
                e.status != 403
                or e.data["message"] != "Resource not accessible by integration"
            ):
                raise

        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        generic_checks |= set(
            [GenericCheck(s.context, s.state) for s in self._get_statuses()]
        )
        return generic_checks

    def _resolve_login(self, name):
        if not name:
            return []
        elif not isinstance(name, str):
            return [name]
        elif name[0] != "@":
            return [name]

        if "/" in name:
            organization, _, team_slug = name.partition("/")
            if not team_slug or "/" in team_slug:
                # Not a team slug
                return [name]
            organization = organization[1:]
        else:
            organization = self.g_pull.base.repo.owner.login
            team_slug = name[1:]

        try:
            g_organization = self.g.get_organization(organization)
            for team in g_organization.get_teams():
                if team.slug == team_slug:
                    return [m.login for m in team.get_members()]
        except github.GithubException as e:
            if e.status >= 500:
                raise
            LOG.warning(
                "fail to get the organization, team or members",
                team=name,
                status=e.status,
                detail=e.data["message"],
                pull_request=self,
            )
        return [name]

    def resolve_teams(self, values):
        if not values:
            return []
        if not isinstance(values, (list, tuple)):
            values = [values]
        return list(itertools.chain.from_iterable((map(self._resolve_login, values))))

    UNUSABLE_STATES = ["unknown", None]

    # NOTE(sileht): quickly retry, if we don't get the status on time
    # the exception is recatch in worker.py, so celery will retry it later
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_exception_type(exceptions.MergeableStateUnknown),
        reraise=True,
    )
    def _ensure_mergable_state(self, force=False):
        if self.g_pull.state == "closed":
            return
        if not force and self.g_pull.mergeable_state not in self.UNUSABLE_STATES:
            return

        # Github is currently processing this PR, we wait the completion
        LOG.info("refreshing", pull_request=self)

        # NOTE(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change, so we get a fresh pull request instead
        # of using update()
        self.g_pull = self.g_pull.base.repo.get_pull(self.g_pull.number)
        if (
            self.g_pull.state == "closed"
            or self.g_pull.mergeable_state not in self.UNUSABLE_STATES
        ):
            return

        raise exceptions.MergeableStateUnknown(self)

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_never,
    )
    def _wait_for_sha_change(self, old_sha):
        if self.g_pull.state == "closed" or self.g_pull.head.sha != old_sha:
            return

        # Github is currently processing this PR, we wait the completion
        LOG.info("refreshing", pull_request=self)

        # NOTE(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change, so we get a fresh pull request instead
        # of using update()
        self.g_pull = self.g_pull.base.repo.get_pull(self.g_pull.number)
        if self.g_pull.state == "closed" or self.g_pull.head.sha != old_sha:
            return
        raise tenacity.TryAgain

    def wait_for_sha_change(self):
        old_sha = self.g_pull.head.sha
        self._wait_for_sha_change(old_sha)
        self._ensure_mergable_state()

    def base_is_modifiable(self):
        return (
            self.g_pull.raw_data["maintainer_can_modify"]
            or self.g_pull.head.repo.id == self.g_pull.base.repo.id
        )

    def is_behind(self):
        branch = self.g_pull.base.repo.get_branch(
            parse.quote(self.g_pull.base.ref, safe="")
        )
        for commit in self.g_pull.get_commits():
            for parent in commit.parents:
                if parent.sha == branch.commit.sha:
                    return False
        return True

    def get_merge_commit_message(self):
        if not self.g_pull.body:
            return

        found = False
        message_lines = []

        for line in self.g_pull.body.split("\n"):
            if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                found = True
            elif found and MARKDOWN_TITLE_RE.match(line):
                break
            elif found:
                message_lines.append(line)

        if found and message_lines:
            return {
                "commit_title": message_lines[0],
                "commit_message": "\n".join(message_lines[1:]).strip(),
            }

    def __str__(self):
        return "%(login)s/%(repo)s/pull/%(number)d@%(branch)s " "s:%(pr_state)s" % {
            "login": self.g_pull.base.user.login,
            "repo": self.g_pull.base.repo.name,
            "number": self.g_pull.number,
            "branch": self.g_pull.base.ref,
            "pr_state": (
                "merged"
                if self.g_pull.merged
                else (self.g_pull.mergeable_state or "none")
            ),
        }
