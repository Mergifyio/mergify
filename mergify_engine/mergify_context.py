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

import itertools
import re
from urllib import parse

import attr
import daiquiri
import httpx
import tenacity

from mergify_engine import check_api
from mergify_engine import exceptions
from mergify_engine import functools_bp


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


@attr.s()
class MergifyContext(object):
    client = attr.ib()
    pull = attr.ib()
    _consolidated_data = attr.ib(init=False, default=None)

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.pull["user"]["login"]
            if "user" in self.pull
            else "<unknown-yet>",
            gh_repo=(
                self.pull["base"]["repo"]["name"]
                if "base" in self.pull
                else "<unknown-yet>"
            ),
            gh_private=(
                self.pull["base"]["repo"]["private"]
                if "base" in self.pull
                else "<unknown-yet>"
            ),
            gh_branch=self.pull["base"]["ref"]
            if "base" in self.pull
            else "<unknown-yet>",
            gh_pull=self.pull["number"],
            gh_pull_url=self.pull.get("html_url", "<unknown-yet>"),
            gh_pull_state=(
                "merged"
                if self.pull.get("merged")
                else (self.pull.get("mergeable_state", "unknown") or "none")
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

    def has_write_permissions(self, login):
        # TODO(sileht): We should cache that, this is also used in command runner
        return self.client.item(f"collaborators/{login}/permission")["permission"] in [
            "admin",
            "write",
        ]

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
            login for login in collabs if self.has_write_permissions(login)
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
        return {
            # Only use internally attributes
            "_approvals": approvals,
            # Can be used by rules too
            "assignee": [a["login"] for a in self.pull["assignees"]],
            # NOTE(sileht): We put an empty label to allow people to match
            # no label set
            "label": [l["name"] for l in self.pull["labels"]],
            "review-requested": (
                [u["login"] for u in self.pull["requested_reviewers"]]
                + ["@" + t["slug"] for t in self.pull["requested_teams"]]
            ),
            "author": self.pull["user"]["login"],
            "merged-by": (
                self.pull["merged_by"]["login"] if self.pull["merged_by"] else ""
            ),
            "merged": self.pull["merged"],
            "closed": self.pull["state"] == "closed",
            "milestone": (
                self.pull["milestone"]["title"] if self.pull["milestone"] else ""
            ),
            "conflict": self.pull["mergeable_state"] == "dirty",
            "base": self.pull["base"]["ref"],
            "head": self.pull["head"]["ref"],
            "locked": self.pull["locked"],
            "title": self.pull["title"],
            "body": self.pull["body"],
            "files": [f["filename"] for f in self.files],
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
            # NOTE(jd) The Check API set conclusion to None for pending.
            # NOTE(sileht): "pending" statuses are not really trackable, we
            # voluntary drop this event because CIs just sent they status every
            # minutes until the CI pass (at least Travis and Circle CI does
            # that). This was causing a big load on Mergify for nothing useful
            # tracked, and on big projects it can reach the rate limit very
            # quickly.
            "status-success": [
                name for name, state in self.checks.items() if state == "success"
            ],
            "status-failure": [
                name for name, state in self.checks.items() if state == "failure"
            ],
            "status-neutral": [
                name for name, state in self.checks.items() if state == "neutral"
            ],
            # NOTE(sileht): Not handled for now
            # cancelled, timed_out, or action_required
        }

    @functools_bp.cached_property
    def checks(self):
        # NOTE(sileht): conclusion can be one of success, failure, neutral,
        # cancelled, timed_out, or action_required, and  None for "pending"
        checks = dict((c["name"], c["conclusion"]) for c in check_api.get_checks(self))
        # NOTE(sileht): state can be one of error, failure, pending,
        # or success.
        checks.update(
            (s["context"], s["state"])
            for s in self.client.items(
                f"commits/{self.pull['head']['sha']}/status", list_items="statuses"
            )
        )
        return checks

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
            organization = self.pull["base"]["repo"]["owner"]["login"]
            team_slug = name[1:]

        try:
            return [
                member["login"]
                for member in self.client.items(
                    f"/orgs/{organization}/teams/{team_slug}/members"
                )
            ]
        except httpx.HTTPClientSideError as e:
            self.log.warning(
                "fail to get the organization, team or members",
                team=name,
                status=e.status_code,
                detail=e.message,
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
        if not (
            self._is_data_complete()
            and self._is_background_github_processing_completed()
        ):
            self.pull = self.client.item(f"pulls/{self.pull['number']}")

        if not self._is_data_complete():
            self.log.error(
                "/pulls/%s has returned an incomplete payload...",
                self.pull["number"],
                data=self.pull,
            )

        if self._is_background_github_processing_completed():
            return

        raise exceptions.MergeableStateUnknown(self)

    def _is_data_complete(self):
        # NOTE(sileht): If pull request come from /pulls listing or check-runs sometimes,
        # they are incomplete, This ensure we have the complete view
        fields_to_control = (
            "state",
            "mergeable_state",
            "merged_by",
            "merged",
            "merged_at",
        )
        for field in fields_to_control:
            if field not in self.pull:
                return False
        return True

    def _is_background_github_processing_completed(self):
        return (
            self.pull["state"] == "closed"
            or self.pull["mergeable_state"] not in self.UNUSABLE_STATES
        )

    def update(self):
        # TODO(sileht): Remove me,
        # Don't use it, because consolidated data are not updated after that.
        # Only used by merge action for posting an update report after rebase.
        self.pull = self.client.item(f"pulls/{self.pull['number']}")

    @functools_bp.cached_property
    def is_behind(self):
        branch_name_escaped = parse.quote(self.pull["base"]["ref"], safe="")
        branch = self.client.item(f"branches/{branch_name_escaped}")
        for commit in self.commits:
            for parent in commit["parents"]:
                if parent["sha"] == branch["commit"]["sha"]:
                    return False
        return True

    def get_merge_commit_message(self):
        if not self.pull["body"]:
            return

        found = False
        message_lines = []

        for line in self.pull["body"].split("\n"):
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
            "login": self.pull["base"]["user"]["login"],
            "repo": self.pull["base"]["repo"]["name"],
            "number": self.pull["number"],
            "branch": self.pull["base"]["ref"],
            "pr_state": (
                "merged"
                if self.pull["merged"]
                else (self.pull["mergeable_state"] or "none")
            ),
        }

    @functools_bp.cached_property
    def reviews(self):
        return list(self.client.items(f"pulls/{self.pull['number']}/reviews"))

    @functools_bp.cached_property
    def commits(self):
        return list(self.client.items(f"pulls/{self.pull['number']}/commits"))

    @functools_bp.cached_property
    def files(self):
        return list(self.client.items(f"pulls/{self.pull['number']}/files"))

    @property
    def pull_from_fork(self):
        return self.pull["head"]["repo"]["id"] != self.pull["base"]["repo"]["id"]

    @property
    def pull_base_is_modifiable(self):
        return self.pull["maintainer_can_modify"] or not self.pull_from_fork
