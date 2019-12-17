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

import github.GithubObject

from mergify_engine import config
from mergify_engine import utils


class Check(github.GithubObject.NonCompletableGithubObject):  # pragma no cover
    def __repr__(self):
        return self.get__repr__(
            {
                "id": self._id.value,
                "name": self._name.value,
                "head_sha": self._head_sha.value,
                "conclusion": self._conclusion.value,
            }
        )

    @property  # noqa
    def id(self):
        return self._id.value

    @property
    def name(self):
        return self._name.value

    @property
    def head_sha(self):
        return self._head_sha.value

    @property
    def conclusion(self):
        return self._conclusion.value

    @property
    def output(self):
        return self._output.value

    @property
    def status(self):
        return self._status.value

    @property
    def completed_at(self):
        return self._completed_at.value

    def _initAttributes(self):
        self._id = github.GithubObject.NotSet
        self._name = github.GithubObject.NotSet
        self._head_sha = github.GithubObject.NotSet
        self._conclusion = github.GithubObject.NotSet
        self._completed_at = github.GithubObject.NotSet
        self._status = github.GithubObject.NotSet
        self._output = github.GithubObject.NotSet
        self._status = github.GithubObject.NotSet

    def _useAttributes(self, attributes):
        if "id" in attributes:
            self._id = self._makeIntAttribute(attributes["id"])
        if "name" in attributes:
            self._name = self._makeStringAttribute(attributes["name"])
        if "head_sha" in attributes:
            self._head_sha = self._makeStringAttribute(attributes["head_sha"])
        if "output" in attributes:
            self._output = self._makeDictAttribute(attributes["output"])
        if "status" in attributes:
            self._status = self._makeStringAttribute(attributes["status"])
        if "conclusion" in attributes:
            self._conclusion = self._makeStringAttribute(attributes["conclusion"])
        if "completed_at" in attributes:
            self._completed_at = self._makeStringAttribute(attributes["completed_at"])


def get_checks_for_ref(repo, sha, parameters=None, mergify_only=False):
    checks = github.PaginatedList.PaginatedList(
        Check,
        repo._requester,
        "%s/commits/%s/check-runs" % (repo.url, sha),
        parameters,
        list_item="check_runs",
        headers={"Accept": "application/vnd.github.antiope-preview+json"},
    )

    if mergify_only:
        checks = [c for c in checks if c._rawData["app"]["id"] == config.INTEGRATION_ID]
    else:
        checks = list(checks)

    # FIXME(sileht): We currently have some issue to set back
    # conclusion to null, Maybe a GH bug or not.
    # As we rely heavily on conclusion to known if we have something to
    # evaluate or not, here a workaround:
    for check in checks:
        if check.status == "in_progress":
            check._useAttributes({"conclusion": None})

    return checks


def get_checks(pull, parameters=None, mergify_only=False):
    return get_checks_for_ref(pull.base.repo, pull.head.sha, parameters, mergify_only)


def get_check_suite(g_repo, check_suite_id):
    _, data = g_repo._requester.requestJsonAndCheck(
        "GET",
        g_repo.url + "/check-suites/" + str(check_suite_id),
        headers={"Accept": "application/vnd.github.antiope-preview+json"},
    )
    return data


def compare_dict(d1, d2, keys):
    for key in keys:
        if d1.get(key) != d2.get(key):
            return False
    return True


def set_check_run(pull, name, status, conclusion=None, output=None):
    post_parameters = {"name": name, "head_sha": pull.head.sha, "status": status}
    if conclusion:
        post_parameters["conclusion"] = conclusion
    if output:
        # Maximum output/summary length for Check API is 65535
        summary = output.get("summary")
        if summary and len(summary) > 65535:
            output["summary"] = utils.unicode_truncate(summary, 65532)
            output["summary"] += "…"  # this is 3 bytes long
        post_parameters["output"] = output

    post_parameters["started_at"] = utils.utcnow().isoformat()
    post_parameters["details_url"] = "%s/checks" % pull.html_url

    if status == "completed":
        post_parameters["completed_at"] = utils.utcnow().isoformat()

    checks = list(
        c
        for c in get_checks(pull, {"check_name": name})
        if c._rawData["app"]["id"] == config.INTEGRATION_ID
    )

    if not checks:
        headers, data = pull._requester.requestJsonAndCheck(
            "POST",
            "%s/check-runs" % (pull.base.repo.url),
            input=post_parameters,
            headers={"Accept": "application/vnd.github.antiope-preview+json"},
        )
        checks = [Check(pull._requester, headers, data, completed=True)]

    if len(checks) > 1:
        pull.log.warning(
            "Multiple mergify checks have been created, " "we got the known race.",
        )

    post_parameters["details_url"] += "?check_run_id=%s" % checks[0].id

    # FIXME(sileht): We have no (simple) way to ensure we don't have multiple
    # worker doing POST at the same time. It's unlike to happen, but it has
    # happen once, so to ensure Mergify continue to work, we update all
    # checks. User will see the check twice for a while, but it's better than
    # having Mergify stuck
    for check in checks:
        # Don't do useless update
        if compare_dict(
            post_parameters,
            check.raw_data,
            ("name", "head_sha", "status", "conclusion", "details_url"),
        ):
            if check.output == output:
                continue
            elif (
                check.output is not None
                and output is not None
                and compare_dict(output, check.output, ("title", "summary"))
            ):
                continue

        headers, data = pull._requester.requestJsonAndCheck(
            "PATCH",
            "%s/check-runs/%s" % (pull.base.repo.url, check.id),
            input=post_parameters,
            headers={"Accept": "application/vnd.github.antiope-preview+json"},
        )
        check = Check(pull._requester, headers, data, completed=True)

    return check
