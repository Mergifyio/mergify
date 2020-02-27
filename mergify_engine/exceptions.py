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


class RateLimited(Exception):
    def __init__(self, countdown):
        self.countdown = countdown


class MergeableStateUnknown(Exception):
    def __init__(self, pull):
        self.pull = pull


BASE_RETRY_TIMEOUT = 60


def need_retry(exception):  # pragma: no cover
    if isinstance(exception, RateLimited):
        return exception.countdown
    elif isinstance(exception, MergeableStateUnknown):
        return BASE_RETRY_TIMEOUT
    elif (
        (isinstance(exception, github.GithubException) and exception.status >= 500)
        or (
            isinstance(exception, requests.exceptions.HTTPError)
            and exception.response.status_code >= 500
        )
        or isinstance(exception, requests.exceptions.ConnectionError)
        or isinstance(exception, requests.exceptions.Timeout)
        or isinstance(exception, requests.exceptions.TooManyRedirects)
    ):
        return BASE_RETRY_TIMEOUT

    # NOTE(sileht): Most of the times token are just temporary invalid, Why ?
    # no idea, ask Github...
    elif isinstance(exception, github.GithubException):
        # Bad creds or token expired, we can't really known
        if exception.status == 401:
            return BASE_RETRY_TIMEOUT
        # Rate limit or abuse detection mechanism, futures events will be rate limited
        # correctly by mergify_engine.utils.Github()
        elif exception.status == 403:
            return BASE_RETRY_TIMEOUT * 5
