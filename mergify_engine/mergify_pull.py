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
import github as pygithub
import tenacity

from mergify_engine import check_api
from mergify_engine import exceptions
from mergify_engine import functools_bp
from mergify_engine import utils
from mergify_engine.clients import github


MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)


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
    client = attr.ib()
    data = attr.ib()
    _consolidated_data = attr.ib(init=False, default=None)

    @property
    def g(self):
        # NOTE(sileht): Remove me
        return utils.Github(self.installation_token)

    @property
    def g_pull(self):
        # NOTE(sileht): Remove me
        return pygithub.PullRequest.PullRequest(
            self.g._Github__requester, {}, self.data, completed=True
        )

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.data["user"]["login"],
            gh_repo=self.data["base"]["repo"]["name"],
            gh_private=self.data["base"]["repo"]["private"],
            gh_branch=self.data["base"]["ref"],
            gh_pull=self.data["number"],
            gh_pull_url=self.data["html_url"],
            gh_pull_state=(
                "merged"
                if self.data["merged_at"]
                else (self.data.get("mergeable_state", "unknown") or "none")
            ),
        )

    @property
    def installation_id(self):
        # NOTE(sileht): Remove me
        return self.client.installation_id

    @property
    def installation_token(self):
        # TODO(sileht): This is used by Git commands, we should validate it
        # before using it
        return self.client.auth.get_access_token()

    def __attrs_post_init__(self):
        self._ensure_complete()

    def _get_valid_users(self):
        bots = list(
            set(
                [r["user"]["login"] for r in self.reviews if r["user"]["type"] == "Bot"]
            )
        )
        collabs = set(
            [r["user"]["login"] for r in self.reviews if r["user"]["type"] != "Bot"]
        )
        valid_collabs = [
            login
            for login in collabs
            if self.client.item(f"collaborators/{login}/permission")["permission"]
            in ["admin", "write"]
        ]
        return bots + valid_collabs

    def _get_consolidated_reviews(self):
        # Ignore reviews that are not from someone with admin/write permissions
        # And only keep the last review for each user.
        comments = dict()
        approvals = dict()
        valid_users = self._get_valid_users()
        for review in self.reviews:
            if review["user"]["login"] not in valid_users:
                continue
            # Only keep latest review of an user
            if review["state"] == "COMMENTED":
                comments[review["user"]["login"]] = review
            else:
                approvals[review["user"]["login"]] = review
        return list(comments.values()), list(approvals.values())

    def to_dict(self):
        if self._consolidated_data is None:
            self._consolidated_data = self._get_consolidated_data()
        return self._consolidated_data

    def _get_consolidated_data(self):
        comments, approvals = self._get_consolidated_reviews()
        statuses = self._get_checks()
        return {
            # Only use internally attributes
            "_approvals": approvals,
            # Can be used by rules too
            "assignee": [a["login"] for a in self.data["assignees"]],
            # NOTE(sileht): We put an empty label to allow people to match
            # no label set
            "label": [l["name"] for l in self.data["labels"]],
            "review-requested": (
                [u["login"] for u in self.data["requested_reviewers"]]
                + ["@" + t["slug"] for t in self.data["requested_teams"]]
            ),
            "author": self.data["user"]["login"],
            "merged-by": (
                self.data["merged_by"]["login"] if self.data["merged_by"] else ""
            ),
            "merged": self.data["merged"],
            "closed": self.data["state"] == "closed",
            "milestone": (
                self.data["milestone"]["title"] if self.data["milestone"] else ""
            ),
            "conflict": self.data["mergeable_state"] == "dirty",
            "base": self.data["base"]["ref"],
            "head": self.data["head"]["ref"],
            "locked": self.data["locked"],
            "title": self.data["title"],
            "body": self.data["body"],
            "files": [f.filename for f in self.g_pull.get_files()],
            "approved-reviews-by": [
                r["user"]["login"] for r in approvals if r["state"] == "APPROVED"
            ],
            "dismissed-reviews-by": [
                r["user"]["login"] for r in approvals if r["state"] == "DISMISSED"
            ],
            "changes-requested-reviews-by": [
                r["user"]["login"]
                for r in approvals
                if r["state"] == "CHANGES_REQUESTED"
            ],
            "commented-reviews-by": [
                r["user"]["login"] for r in comments if r["state"] == "COMMENTED"
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
        except pygithub.GithubException as e:
            if (
                e.status != 403
                or e.data["message"] != "Resource not accessible by integration"
            ):
                raise

        statuses = list(
            self.client.items(
                f"commits/{self.data['head']['sha']}/status", list_items="statuses"
            )
        )

        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        generic_checks |= set(
            [GenericCheck(s["context"], s["state"]) for s in statuses]
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
            organization = self.data["base"]["repo"]["owner"]["login"]
            team_slug = name[1:]

        try:
            g_organization = self.g.get_organization(organization)
            for team in g_organization.get_teams():
                if team.slug == team_slug:
                    return [m.login for m in team.get_members()]
        except pygithub.GithubException as e:
            if e.status >= 500:
                raise
            self.log.warning(
                "fail to get the organization, team or members",
                team=name,
                status=e.status,
                detail=e.data["message"],
            )
        return [name]

    def resolve_teams(self, values):
        if not values:
            return []
        if not isinstance(values, (list, tuple)):
            values = [values]
        values = list(itertools.chain.from_iterable((map(self._resolve_login, values))))
        return values

    UNUSABLE_STATES = ["unknown", None]

    # NOTE(sileht): quickly retry, if we don't get the status on time
    # the exception is recatch in worker.py, so celery will retry it later
    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=0.2),
        stop=tenacity.stop_after_attempt(5),
        retry=tenacity.retry_if_exception_type(exceptions.MergeableStateUnknown),
        reraise=True,
    )
    def _ensure_complete(self):
        if not self._is_complete():
            self.data = self.client.item(f"pulls/{self.data['number']}")

        if self._is_complete():
            return

        raise exceptions.MergeableStateUnknown(self)

    def _is_complete(self):
        # NOTE(sileht): If pull request come from /pulls listing or check-runs sometimes,
        # they are incomplete, This ensure we have the complete view
        return (
            self.data.get("state") == "closed"
            or self.data.get("mergeable_state") not in self.UNUSABLE_STATES
        )

    def update(self):
        # TODO(sileht): Remove me,
        # Don't use it, because consolidated data are not updated after that.
        # Only used by merge action for posting an update report after rebase.
        self.data = self.client.item(f"pulls/{self.data['number']}")

    @functools_bp.cached_property
    def is_behind(self):
        branch = self.g_pull.base.repo.get_branch(
            parse.quote(self.data["base"]["ref"], safe="")
        )
        for commit in self.commits:
            for parent in commit["parents"]:
                if parent["sha"] == branch.commit.sha:
                    return False
        return True

    def get_merge_commit_message(self):
        if not self.data["body"]:
            return

        found = False
        message_lines = []

        for line in self.data["body"].split("\n"):
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
            "login": self.data["base"]["user"]["login"],
            "repo": self.data["base"]["repo"]["name"],
            "number": self.data["number"],
            "branch": self.data["base"]["ref"],
            "pr_state": (
                "merged"
                if self.data["merged"]
                else (self.data["mergeable_state"] or "none")
            ),
        }

    @functools_bp.cached_property
    def reviews(self):
        return list(self.client.items(f"pulls/{self.data['number']}/reviews"))

    @functools_bp.cached_property
    def commits(self):
        return list(self.client.items(f"pulls/{self.data['number']}/commits"))

    # NOTE(sileht): map all attributes that in theory doesn't do http calls

    @property
    def number(self):
        return self.data["number"]

    @property
    def title(self):
        return self.data["title"]

    @property
    def user(self):
        return self.data["user"]["login"]

    @property
    def state(self):
        return self.data["state"]

    @property
    def from_fork(self):
        return self.data["head"]["repo"]["id"] != self.data["base"]["repo"]["id"]

    @property
    def base_is_modifiable(self):
        return self.data["maintainer_can_modify"] or not self.from_fork

    @property
    def merge_commit_sha(self):
        return self.data["merge_commit_sha"]

    @property
    def head_sha(self):
        return self.data["head"]["sha"]

    @property
    def base_ref(self):
        return self.data["base"]["ref"]

    @property
    def head_ref(self):
        return self.data["head"]["ref"]

    @property
    def base_repo_name(self):
        return self.data["base"]["repo"]["name"]

    @property
    def head_repo_name(self):
        return self.data["head"]["repo"]["name"]

    @property
    def base_repo_owner_login(self):
        return self.data["base"]["repo"]["owner"]["login"]

    @property
    def head_repo_owner_login(self):
        return self.data["head"]["repo"]["owner"]["login"]

    @property
    def mergeable_state(self):
        return self.data["mergeable_state"]

    @property
    def merged(self):
        return self.data["merged"]

    @property
    def merged_by(self):
        return self.data["merged_by"]["login"]
