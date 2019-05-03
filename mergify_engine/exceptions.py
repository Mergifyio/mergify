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

import github

import requests


class MergeableStateUnknown(Exception):
    pass


def need_retry(exception):
    if isinstance(exception, MergeableStateUnknown):
        return 30
    elif ((isinstance(exception, github.GithubException) and
           exception.status >= 500) or
          (isinstance(exception, requests.exceptions.HTTPError) and
           exception.response.status_code >= 500) or
          isinstance(exception, requests.exceptions.ConnectionError)):
        return 30

    # NOTE(sileht): Most of the times token are just temporary invalid, Why ?
    # no idea, ask Github...
    elif (isinstance(exception, github.GithubException) and
            exception.status == 401):
        return 10

    elif (isinstance(exception, github.GithubException) and
          exception.status == 403 and
          ("You have triggered an abuse detection mechanism" in
           exception.data["message"] or
           exception.data["message"].startswith("API rate limit exceeded"))
          ):
        return 60 * 5

    elif isinstance(exception, github.RateLimitExceededException):
        return 60 * 5
