# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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

import logging

import github

from mergify_engine import config
from mergify_engine import gh_pr_fullifier
from mergify_engine import gh_update_branch

LOG = logging.getLogger(__name__)


def pretty(self):
    extra = getattr(self, "mergify_engine", {})
    required_statuses_succeed = extra.get("required_statuses_succeed", "nc")
    status = extra.get("status", {})
    approvals = len(extra["approvals"][0]) if "approvals" in extra else "nc"
    return "%s/%s/pull/%s@%s (%s/%s/%s/%s/%s/%s)" % (
        self.base.user.login,
        self.base.repo.name,
        self.number,
        self.base.ref,
        ("merged" if self.merged
         else (self.mergeable_state or "none")),
        required_statuses_succeed,
        approvals,
        status.get("mergify_state", 'nc'),
        status.get("github_state", 'nc'),
        status.get("github_description", 'nc'),
    )


def mergify_engine_github_post_check_status(self, redis, installation_id,
                                            state, msg, context=None):

    context = "pr" if context is None else context
    msg_key = "%s/%s/%d/%s" % (installation_id, self.base.repo.full_name,
                               self.number, context)

    if len(msg) >= 140:
        description = msg[0:137] + "..."
        redis.hset("status", msg_key, msg.encode('utf8'))
        target_url = "%s/check_status_msg/%s" % (config.BASE_URL, msg_key)
    else:
        description = msg
        target_url = None

    LOG.info("%s set status to %s (%s)", self.pretty(), state, description)
    # NOTE(sileht): We can't use commit.create_status() because
    # if use the head repo instead of the base repo
    try:
        self._requester.requestJsonAndCheck(
            "POST",
            self.base.repo.url + "/statuses/" + self.head.sha,
            input={'state': state,
                   'description': description,
                   'target_url': target_url,
                   'context': "%s/%s" % (config.CONTEXT, context)},
            headers={'Accept':
                     'application/vnd.github.machine-man-preview+json'}
        )
    except github.GithubException as e:  # pragma: no cover
        LOG.exception("%s set status fail: %s",
                      self.pretty(), e.data["message"])


def mergify_engine_merge(self, rule):
    post_parameters = {
        "sha": self.head.sha,
        "merge_method": rule["merge_strategy"]["method"],
    }
    # FIXME(sileht): use self.merge when it will
    # support sha and merge_method arguments
    try:
        headers, data = self._requester.requestJsonAndCheck(
            "PUT", self.url + "/merge", input=post_parameters)
    except github.GithubException as e:   # pragma: no cover
        if (self.is_merged() and
                e.data["message"] == "Pull Request is not mergeable"):
            # Not a big deal, we will received soon the pull_request/close
            # event
            LOG.info("%s: was merged in the meantime")
            return True

        fallback = rule["merge_strategy"]["rebase_fallback"]
        if (e.data["message"] != "This branch can't be rebased" or
                rule["merge_strategy"]["method"] != "rebase" or
                fallback == "none"):
            LOG.exception("%s merge fail: %d, %s",
                          self.pretty(), e.status, e.data["message"])
            return False

        # If rebase fail retry with merge
        post_parameters['merge_method'] = fallback
        try:
            headers, data = self._requester.requestJsonAndCheck(
                "PUT", self.url + "/merge", input=post_parameters)
        except github.GithubException as e:
            LOG.exception("%s merge fail: %d, %s",
                          self.pretty(), e.status, e.data["message"])
            return False

        # FIXME(sileht): depending on the kind of failure we can endloop
        # to try to merge the pr again and again.
        # to repoduce the issue
    return True


def base_is_modifiable(self):
    return (self.raw_data["maintainer_can_modify"] or
            self.head.repo.id == self.base.repo.id)


def from_event(repo, data):
    # TODO(sileht): do it only once in handle()
    # NOTE(sileht): Convert event payload, into pygithub object
    # instead of querying the API
    if "pull_request" in data:
        return github.PullRequest.PullRequest(
            repo._requester, {}, data["pull_request"], completed=True)


def from_cache(repo, data, **extra):
    # NOTE(sileht): Reload our PullRequest custom object from cache data
    # instead of querying the API
    p = github.PullRequest.PullRequest(
        repo._requester, {}, data, completed=True)
    return p.fullify(data, **extra)


def monkeypatch_github():
    p = github.PullRequest.PullRequest

    p.pretty = pretty
    p.fullify = gh_pr_fullifier.fullify
    p.jsonify = gh_pr_fullifier.jsonify

    p.base_is_modifiable = base_is_modifiable
    p.mergify_engine_merge = mergify_engine_merge
    p.mergify_engine_github_post_check_status = \
        mergify_engine_github_post_check_status

    # Missing Github API
    p.mergify_engine_update_branch = gh_update_branch.update_branch

    # FIXME(sileht): remove me, used by engine for sorting pulls
    p.mergify_engine_sort_status = property(
        lambda p: p.mergify_engine["status"]["mergify_state"])

    # FIXME(sileht): Workaround https://github.com/PyGithub/PyGithub/issues/660
    github.PullRequestReview.PullRequestReview._completeIfNeeded = (
        lambda self: None)
