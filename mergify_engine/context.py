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

import functools
import itertools
import operator
import re
from urllib import parse

import attr
import cachetools
import daiquiri
import httpx
import tenacity

from mergify_engine import check_api
from mergify_engine import config
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
class Context(object):
    client = attr.ib()
    pull = attr.ib()
    sources = attr.ib(factory=list)
    _write_permission_cache = attr.ib(factory=lambda: cachetools.LRUCache(4096))

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.pull["base"]["user"]["login"]
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

    def __attrs_post_init__(self):
        self._ensure_complete()

    @cachetools.cachedmethod(
        cache=operator.attrgetter("_write_permission_cache"),
        key=functools.partial(cachetools.keys.hashkey, "has_write_permissions"),
    )
    def has_write_permissions(self, login):
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

    @functools_bp.cached_property
    def consolidated_reviews(self):
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

    def get_consolidated_data(self, name):
        if name == "assignee":
            return [a["login"] for a in self.pull["assignees"]]

        elif name == "label":
            return [l["name"] for l in self.pull["labels"]]

        elif name == "review-requested":
            return [u["login"] for u in self.pull["requested_reviewers"]] + [
                "@" + t["slug"] for t in self.pull["requested_teams"]
            ]

        elif name == "draft":
            return self.pull["draft"]

        elif name == "author":
            return self.pull["user"]["login"]

        elif name == "merged-by":
            return self.pull["merged_by"]["login"] if self.pull["merged_by"] else ""

        elif name == "merged":
            return self.pull["merged"]

        elif name == "closed":
            return self.pull["state"] == "closed"

        elif name == "milestone":
            return self.pull["milestone"]["title"] if self.pull["milestone"] else ""

        elif name == "conflict":
            return self.pull["mergeable_state"] == "dirty"

        elif name == "base":
            return self.pull["base"]["ref"]

        elif name == "head":
            return self.pull["head"]["ref"]

        elif name == "locked":
            return self.pull["locked"]

        elif name == "title":
            return self.pull["title"]

        elif name == "body":
            return self.pull["body"]

        elif name == "files":
            return [f["filename"] for f in self.files]

        elif name == "approved-reviews-by":
            _, approvals = self.consolidated_reviews
            return [r["user"]["login"] for r in approvals if r["state"] == "APPROVED"]
        elif name == "dismissed-reviews-by":
            _, approvals = self.consolidated_reviews
            return [r["user"]["login"] for r in approvals if r["state"] == "DISMISSED"]
        elif name == "changes-requested-reviews-by":
            _, approvals = self.consolidated_reviews
            return [
                r["user"]["login"]
                for r in approvals
                if r["state"] == "CHANGES_REQUESTED"
            ]
        elif name == "commented-reviews-by":
            comments, _ = self.consolidated_reviews
            return [r["user"]["login"] for r in comments if r["state"] == "COMMENTED"]

        # NOTE(jd) The Check API set conclusion to None for pending.
        # NOTE(sileht): "pending" statuses are not really trackable, we
        # voluntary drop this event because CIs just sent they status every
        # minutes until the CI pass (at least Travis and Circle CI does
        # that). This was causing a big load on Mergify for nothing useful
        # tracked, and on big projects it can reach the rate limit very
        # quickly.
        # NOTE(sileht): Not handled for now: cancelled, timed_out, or action_required
        elif name == "status-success":
            return [ctxt for ctxt, state in self.checks.items() if state == "success"]
        elif name == "status-failure":
            return [ctxt for ctxt, state in self.checks.items() if state == "failure"]
        elif name == "status-neutral":
            return [ctxt for ctxt, state in self.checks.items() if state == "neutral"]

        else:
            raise AttributeError(f"Invalid attribute: {name}")

    def update_pull_check_runs(self, check):
        self.pull_check_runs = [
            c for c in self.pull_check_runs if c["name"] != check["name"]
        ]
        self.pull_check_runs.append(check)

    @functools_bp.cached_property
    def pull_check_runs(self):
        return check_api.get_checks_for_ref(self, self.pull["head"]["sha"])

    @property
    def pull_engine_check_runs(self):
        return [
            c for c in self.pull_check_runs if c["app"]["id"] == config.INTEGRATION_ID
        ]

    @functools_bp.cached_property
    def checks(self):
        # NOTE(sileht): conclusion can be one of success, failure, neutral,
        # cancelled, timed_out, or action_required, and  None for "pending"
        checks = dict((c["name"], c["conclusion"]) for c in self.pull_check_runs)
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
        try:
            del self.__dict__["pull_check_runs"]
        except KeyError:
            pass

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
