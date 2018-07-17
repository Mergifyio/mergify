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

import tenacity


class Check(github.GithubObject.NonCompletableGithubObject):  # pragma no cover
    def __repr__(self):
        return self.get__repr__({
            "id": self._id.value,
            "name": self._name.value,
            "conclusion": self._conclusion.value,
        })

    @property
    def name(self):
        return self._name.value

    @property
    def conclusion(self):
        return self._conclusion.value

    def _initAttributes(self):
        self._id = github.GithubObject.NotSet
        self._name = github.GithubObject.NotSet
        self._conclusion = github.GithubObject.NotSet

    def _useAttributes(self, attributes):
        if "id" in attributes:
            self._id = self._makeStringAttribute(attributes["id"])
        if "name" in attributes:
            self._name = self._makeStringAttribute(attributes["name"])
        if "conclusion" in attributes:
            self._conclusion = self._makeStringAttribute(
                attributes["conclusion"])


def get_checks(pull):
    return github.PaginatedList.PaginatedList(
        Check, pull._requester,
        "%s/commits/%s/check-runs" % (pull.base.repo.url, pull.head.sha),
        None,
        list_item='check_runs',
        headers={'Accept': 'application/vnd.github.antiope-preview+json'}
    )


def get_check_suite(g_repo, check_suite_id):
    _, data = g_repo._requester.requestJsonAndCheck(
        "GET", g_repo.url + "/check-suites/" + str(check_suite_id),
        headers={'Accept': 'application/vnd.github.antiope-preview+json'}
    )
    return data


@tenacity.retry(wait=tenacity.wait_exponential(multiplier=0.2),
                stop=tenacity.stop_after_attempt(5),
                retry=tenacity.retry_never)
def workaround_for_unfinished_check_suite(g_repo, data):
    """Workaround for broken Checks API events.

    The Checks API have two major flaws:
    * We received check_suite/completed too early, some checks are still
    pending
    * Once it's completed, even if some check are rerun, we never got
    a new event to notify us that it finish again.

    This method workaround the first issue, but the second one is still
    unsolved
    """
    check_suite = data["check_suite"]
    check_suite = get_check_suite(g_repo, check_suite["id"])
    if check_suite["conclusion"]:
        # NOTE(sileht): even when we got the conclusion the status is
        # still pending... Well this API is clearly not ready for prime
        # time
        data["check_suite"] = check_suite
        return data
    raise tenacity.TryAgain
