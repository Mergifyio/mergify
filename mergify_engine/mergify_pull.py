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

import attr

import daiquiri

import github

import tenacity

from mergify_engine import check_api
from mergify_engine import config


class MergeableStateUnknown(Exception):
    pass


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

    @classmethod
    def from_raw(cls, installation_id, installation_token, pull_raw):
        g = github.Github(installation_token,
                          base_url="https://api.%s" % config.GITHUB_DOMAIN)
        pull = github.PullRequest.PullRequest(g._Github__requester, {},
                                              pull_raw, completed=True)
        return cls(g, pull, installation_id)

    @classmethod
    def from_number(cls, installation_id, installation_token, owner, reponame,
                    pull_number):
        g = github.Github(installation_token,
                          base_url="https://api.%s" % config.GITHUB_DOMAIN)
        repo = g.get_repo(owner + "/" + reponame)
        pull = repo.get_pull(pull_number)
        return cls(g, pull, installation_id)

    def __attrs_post_init__(self):
        self.log = daiquiri.getLogger(__name__, pull_request=self)
        self._ensure_mergable_state()

    def _get_perm(self, login):
        return self.g_pull.base.repo.get_collaborator_permission(login)

    def _get_reviews(self):
        # Ignore reviews that are not from someone with admin/write permissions
        # And only keep the last review for each user.
        reviews = dict((review.user.login, review)
                       for review in self.g_pull.get_reviews())
        return list(review for login, review in reviews.items()
                    if self._get_perm(login) in ["admin", "write"])

    def to_dict(self):
        reviews = self._get_reviews()
        statuses = self._get_checks()
        # FIXME(jd) pygithub does 2 HTTP requests whereas 1 is enough!
        review_requested_users, review_requested_teams = (
            self.g_pull.get_review_requests()
        )
        return {
            "assignee": [a.login for a in self.g_pull.assignees],
            "label": [l.name for l in self.g_pull.labels],
            "review-requested": (
                [u.login for u in review_requested_users] +
                ["@" + t.slug for t in review_requested_teams]
            ),
            "author": self.g_pull.user.login,
            "merged-by": (
                self.g_pull.merged_by.login if self.g_pull.merged_by else ""
            ),
            "merged": self.g_pull.merged,
            "closed": self.g_pull.state == "closed",
            "milestone": (
                self.g_pull.milestone.title if self.g_pull.milestone else ""
            ),
            "base": self.g_pull.base.ref,
            "head": self.g_pull.head.ref,
            "locked": self.g_pull._rawData['locked'],
            "title": self.g_pull.title,
            "body": self.g_pull.body,
            "files": [f.filename for f in self.g_pull.get_files()],
            "approved-reviews-by": [r.user.login for r in reviews
                                    if r.state == "APPROVED"],
            "dismissed-reviews-by": [r.user for r in reviews
                                     if r.state == "DISMISSED"],
            "changes-requested-reviews-by": [
                r.user for r in reviews if r.state == "CHANGES_REQUESTED"
            ],
            "commented-reviews-by": [r.user for r in reviews
                                     if r.state == "COMMENTED"],
            "status-success": [s.context for s in statuses
                               if s.state == "success"],
            # NOTE(jd) The Check API set conclusion to None for pending.
            # NOTE(sileht): "pending" statuses are not really trackable, we
            # voluntary drop this event because CIs just sent they status every
            # minutes until the CI pass (at least Travis and Circle CI does
            # that). This was causing a big load on Mergify for nothing useful
            # tracked, and on big projects it can reach the rate limit very
            # quickly.
            # "status-pending": [s.context for s in statuses
            #                    if s.state in ("pending", None)],
            "status-failure": [s.context for s in statuses
                               if s.state == "failure"],
        }

    def _get_statuses(self):
        already_seen = set()
        statuses = []
        for status in github.PaginatedList.PaginatedList(
                github.CommitStatus.CommitStatus,
                self.g_pull._requester,
                self.g_pull.base.repo.url + "/commits/" +
                self.g_pull.head.sha + "/statuses",
                None
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
            generic_checks |= set([GenericCheck(c.name, c.conclusion)
                                   for c in check_api.get_checks(self.g_pull)])
        except github.GithubException as e:
            if (e.status != 403 or e.data["message"] !=
                    "Resource not accessible by integration"):
                raise

        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        generic_checks |= set([GenericCheck(s.context, s.state)
                               for s in self._get_statuses()])
        return generic_checks

    def _resolve_login(self, name):
        organization, _, team_slug = name.partition("/")
        if organization[0] != "@" or not team_slug or '/' in team_slug:
            # Not a team slug
            return [name]

        try:
            g_organization = self.g.get_organization(organization[1:])
            for team in g_organization.get_teams():
                if team.slug == team_slug:
                    return [m.login for m in team.get_members()]
        except github.GithubException as e:
            if e.status >= 500:
                raise
            self.log.warning("fail to get the organization, team or members",
                             team=name, status=e.status,
                             detail=e.data["message"])
        return [name]

    def resolve_teams(self, values):
        if not values:
            return []
        if not isinstance(values, (list, tuple)):
            values = [values]
        return list(itertools.chain.from_iterable((
            map(self._resolve_login, values))))

    UNUSABLE_STATES = ["unknown", None]

    # NOTE(sileht): quickly retry, if we don't get the status on time
    # the exception is recatch in worker.py, so celery will retry it later
    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=0.2),
                    stop=tenacity.stop_after_attempt(5),
                    retry=tenacity.retry_if_exception_type(
                        MergeableStateUnknown),
                    reraise=True)
    def _ensure_mergable_state(self, force=False):
        if self.g_pull.merged:
            return
        if (not force and
                self.g_pull.mergeable_state not in self.UNUSABLE_STATES):
            return

        # Github is currently processing this PR, we wait the completion
        self.log.info("refreshing")

        # NOTE(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change, so we get a fresh pull request instead
        # of using update()
        self.g_pull = self.g_pull.base.repo.get_pull(self.g_pull.number)
        if (self.g_pull.merged or
                self.g_pull.mergeable_state not in self.UNUSABLE_STATES):
            return

        raise MergeableStateUnknown()

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=0.2),
                    stop=tenacity.stop_after_attempt(5),
                    retry=tenacity.retry_never)
    def _wait_for_sha_change(self, old_sha):
        if (self.g_pull.merged or self.g_pull.head.sha != old_sha):
            return

        # Github is currently processing this PR, we wait the completion
        self.log.info("refreshing")

        # NOTE(sileht): Well github doesn't always update etag/last_modified
        # when mergeable_state change, so we get a fresh pull request instead
        # of using update()
        self.g_pull = self.g_pull.base.repo.get_pull(self.g_pull.number)
        if (self.g_pull.merged or self.g_pull.head.sha != old_sha):
            return
        raise tenacity.TryAgain

    def wait_for_sha_change(self):
        old_sha = self.g_pull.head.sha
        self._wait_for_sha_change(old_sha)
        self._ensure_mergable_state()

    def base_is_modifiable(self):
        return (self.g_pull.raw_data["maintainer_can_modify"] or
                self.g_pull.head.repo.id == self.g_pull.base.repo.id)

    def _merge_failed(self, e):
        # Don't log some  common and valid error
        if e.data["message"].startswith("Base branch was modified"):
            return False

        self.log.error("merge failed",
                       status=e.status, error=e.data["message"],
                       exc_info=True)
        return False

    def merge(self, merge_method, rebase_fallback):
        try:
            self.g_pull.merge(
                sha=self.g_pull.head.sha,
                merge_method=merge_method)
        except github.GithubException as e:   # pragma: no cover
            if (self.g_pull.is_merged() and
                    e.data["message"] == "Pull Request is not mergeable"):
                # Not a big deal, we will receive soon the pull_request close
                # event
                self.log.info("merged in the meantime")
                return True

            if (e.data["message"] != "This branch can't be rebased" or
                    merge_method != "rebase" or
                    (rebase_fallback == "none" or rebase_fallback is None)):
                return self._merge_failed(e)

            # If rebase fail retry with merge
            try:
                self.g_pull.merge(sha=self.g_pull.head.sha,
                                  merge_method=rebase_fallback)
            except github.GithubException as e:
                return self._merge_failed(e)
        return True

    def is_behind(self):
        branch = self.g_pull.base.repo.get_branch(self.g_pull.base.ref)
        for commit in self.g_pull.get_commits():
            for parent in commit.parents:
                if parent.sha == branch.commit.sha:
                    return False
        return True

    def __str__(self):
        return ("%(login)s/%(repo)s/pull/%(number)d@%(branch)s "
                "s:%(pr_state)s" % {
                    "login": self.g_pull.base.user.login,
                    "repo": self.g_pull.base.repo.name,
                    "number": self.g_pull.number,
                    "branch": self.g_pull.base.ref,
                    "pr_state": ("merged" if self.g_pull.merged else
                                 (self.g_pull.mergeable_state or "none")),
                })
