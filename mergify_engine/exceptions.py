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

import httpx


class MergifyNotInstalled(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, countdown, raw_data):
        self.countdown = countdown
        self.raw_data = raw_data


class MergeableStateUnknown(Exception):
    def __init__(self, ctxt):
        self.ctxt = ctxt


RATE_LIMIT_RETRY_MIN = 3
BASE_RETRY_TIMEOUT = 60


def need_retry(exception):  # pragma: no cover
    if isinstance(exception, RateLimited):
        # NOTE(sileht): when we are close to reset date, and since utc time between us and
        # github differ a bit, we can have negative delta, so set a minimun for retrying
        return max(exception.countdown, RATE_LIMIT_RETRY_MIN)
    elif isinstance(exception, MergeableStateUnknown):
        return BASE_RETRY_TIMEOUT
    elif isinstance(exception, httpx.HTTPError) and (
        exception.response is None or exception.response.status_code >= 500
    ):
        # NOTE(sileht): We already retry locally with urllib3, so if we get there, Github
        # is in a really bad shape...
        return BASE_RETRY_TIMEOUT * 5
    # NOTE(sileht): Most of the times token are just temporary invalid, Why ?
    # no idea, ask Github...
    elif isinstance(exception, httpx.HTTPError):
        # Bad creds or token expired, we can't really known
        if exception.response.status_code == 401:
            return BASE_RETRY_TIMEOUT
        # Rate limit or abuse detection mechanism, futures events will be rate limited
        # correctly by mergify_engine.utils.Github()
        elif exception.response.status_code == 403:
            return BASE_RETRY_TIMEOUT * 5
