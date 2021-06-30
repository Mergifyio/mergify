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
import dataclasses
import datetime
import typing

import aredis

from mergify_engine.clients import http


if typing.TYPE_CHECKING:
    from mergify_engine import context


class MergifyNotInstalled(Exception):
    pass


@dataclasses.dataclass
class RateLimited(Exception):
    countdown: datetime.timedelta
    remaining: int


@dataclasses.dataclass
class EngineNeedRetry(Exception):
    pass


@dataclasses.dataclass
class MergeableStateUnknown(EngineNeedRetry):
    ctxt: "context.Context"


RATE_LIMIT_RETRY_MIN = datetime.timedelta(seconds=3)

IGNORED_HTTP_ERRORS: typing.Dict[int, typing.List[str]] = {
    403: [
        "Repository access blocked",  # Blocked Github Account or Repo
        "Resource not accessible by integration",  # missing permission
        "Repository was archived so is read-only",
    ],
    422: [
        "Sorry, there was a problem generating this diff. The repository may be missing relevant data."
    ],
    503: ["Sorry, this diff is taking too long to generate."],
}


def should_be_ignored(exception: Exception) -> bool:
    if isinstance(exception, (http.HTTPClientSideError, http.HTTPServerSideError)):
        for error in IGNORED_HTTP_ERRORS.get(exception.status_code, []):
            if error in exception.message:
                return True

        # NOTE(sileht): a repository return 404 for /pulls..., so can't do much
        if exception.status_code == 404 and exception.request.url.path.endswith(
            "/pulls"
        ):
            return True

        # NOTE(sileht): branch is gone since we started to handle a PR
        elif exception.status_code == 404 and "/branches/" in str(
            exception.request.url
        ):
            return True

    return False


def need_retry(
    exception: Exception,
) -> typing.Optional[datetime.timedelta]:  # pragma: no cover
    if isinstance(exception, RateLimited):
        # NOTE(sileht): when we are close to reset date, and since utc time between us and
        # github differ a bit, we can have negative delta, so set a minimun for retrying
        return max(exception.countdown, RATE_LIMIT_RETRY_MIN)
    elif isinstance(exception, EngineNeedRetry):
        return datetime.timedelta(minutes=1)

    elif isinstance(exception, (http.RequestError, http.HTTPServerSideError)):
        # NOTE(sileht): We already retry locally with urllib3, so if we get there, Github
        # is in a really bad shape...
        return datetime.timedelta(minutes=1)

    # NOTE(sileht): Most of the times token are just temporary invalid, Why ?
    # no idea, ask Github...
    elif isinstance(exception, http.HTTPClientSideError):
        # Bad creds or token expired, we can't really known
        if exception.response.status_code == 401:
            return datetime.timedelta(minutes=1)
        # Rate limit or abuse detection mechanism, futures events will be rate limited
        # correctly by mergify_engine.utils.Github()
        elif exception.response.status_code == 403:
            return datetime.timedelta(minutes=3)

    elif isinstance(exception, aredis.exceptions.ConnectionError):
        return datetime.timedelta(minutes=1)

    return None
