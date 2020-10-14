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

from mergify_engine.clients import http


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
NOT_ACCESSIBLE_REPOSITORY_MESSAGES = [
    "Repository access blocked",  # Blocked Github Account or Repo
    "Resource not accessible by integration",  # missing permission
]

MISSING_REPOSITORY_DATA_MESSAGE = "Sorry, there was a problem generating this diff. The repository may be missing relevant data."


def should_be_ignored(exception):
    if isinstance(exception, http.HTTPClientSideError):
        if (
            exception.status_code == 403
            and exception.message in NOT_ACCESSIBLE_REPOSITORY_MESSAGES
        ):
            return True

        elif (
            exception.status_code == 422
            and exception.message == MISSING_REPOSITORY_DATA_MESSAGE
        ):
            return True

        # NOTE(sileht): a repository return 404 for /pulls..., so can't do much
        elif exception.status_code == 404 and str(exception.request.url).endswith(
            "/pulls"
        ):
            return True

        # NOTE(sileht): branch is gone since we started to handle a PR
        elif exception.status_code == 404 and "/branches/" in str(
            exception.request.url
        ):
            return True

    return False


def need_retry(exception):  # pragma: no cover
    if isinstance(exception, RateLimited):
        # NOTE(sileht): when we are close to reset date, and since utc time between us and
        # github differ a bit, we can have negative delta, so set a minimun for retrying
        return max(exception.countdown, RATE_LIMIT_RETRY_MIN)
    elif isinstance(exception, MergeableStateUnknown):
        return BASE_RETRY_TIMEOUT

    elif isinstance(exception, (http.RequestError, http.HTTPServerSideError)):
        # NOTE(sileht): We already retry locally with urllib3, so if we get there, Github
        # is in a really bad shape...
        return BASE_RETRY_TIMEOUT * 5
    # NOTE(sileht): Most of the times token are just temporary invalid, Why ?
    # no idea, ask Github...
    elif isinstance(exception, http.HTTPClientSideError):
        # Bad creds or token expired, we can't really known
        if exception.response.status_code == 401:
            return BASE_RETRY_TIMEOUT
        # Rate limit or abuse detection mechanism, futures events will be rate limited
        # correctly by mergify_engine.utils.Github()
        elif exception.response.status_code == 403:
            return BASE_RETRY_TIMEOUT * 5
