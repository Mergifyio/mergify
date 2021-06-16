# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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

import json
import os
import sys
import typing

import daiquiri
import httpx
import tenacity
from werkzeug.http import parse_date

from mergify_engine import date


LOG = daiquiri.getLogger(__name__)

DEPLOY_VERSION = os.getenv("HEROKU_RELEASE_VERSION", "dev")
ENGINE_VERSION = os.getenv("HEROKU_SLUG_COMMIT", "dev")[:7]
PYTHON_VERSION = (
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
)
HTTPX_VERSION = httpx.__version__

DEFAULT_HEADERS = {
    "Accept": "application/vnd.github.machine-man-preview+json",
    "User-Agent": f"mergify-engine/{ENGINE_VERSION} deploy/{DEPLOY_VERSION} python/${PYTHON_VERSION} httpx/{HTTPX_VERSION}",
}

DEFAULT_CLIENT_OPTIONS = {
    "headers": DEFAULT_HEADERS,
    "timeout": httpx.Timeout(5.0, read=10.0),
}

HTTPStatusError = httpx.HTTPStatusError
RequestError = httpx.RequestError


class HTTPServerSideError(httpx.HTTPStatusError):
    @property
    def message(self):
        return self.response.text

    @property
    def status_code(self):
        return self.response.status_code


class HTTPClientSideError(httpx.HTTPStatusError):
    @property
    def message(self):
        # TODO(sileht): do something with errors and documentation_url when present
        # https://developer.github.com/v3/#client-errors
        return self.response.json()["message"]

    @property
    def status_code(self):
        return self.response.status_code


class HTTPForbidden(HTTPClientSideError):
    pass


class HTTPUnauthorized(HTTPClientSideError):
    pass


class HTTPNotFound(HTTPClientSideError):
    pass


class HTTPTooManyRequests(HTTPClientSideError):
    pass


class HTTPServiceUnavailable(HTTPServerSideError):
    pass


STATUS_CODE_TO_EXC = {
    401: HTTPUnauthorized,
    403: HTTPForbidden,
    404: HTTPNotFound,
    429: HTTPTooManyRequests,
    503: HTTPServiceUnavailable,
}


def wait_retry_after_header(retry_state):
    exc = retry_state.outcome.exception()
    if exc is None or not getattr(exc, "response", None):
        return 0

    value = exc.response.headers.get("retry-after")
    if value is None:
        return 0
    elif value.isdigit():
        return int(value)

    d = parse_date(value)
    if d is None:
        return 0
    return max(0, (d - date.utcnow()).total_seconds())


def extract_github_extra(client):
    if client.auth and hasattr(client.auth, "owner"):
        return client.auth.owner


def before_log(retry_state):
    client = retry_state.args[0]
    method = retry_state.args[1]
    gh_owner = extract_github_extra(client)
    url = retry_state.args[2]
    LOG.debug(
        "http request starts",
        method=method,
        url=url,
        gh_owner=gh_owner,
        attempts=retry_state.attempt_number,
    )


def after_log(retry_state):
    client = retry_state.args[0]
    method = retry_state.args[1]
    url = retry_state.args[2]
    gh_owner = extract_github_extra(client)
    error_message = None
    response = None
    exc_info = None
    if retry_state.outcome.failed:
        exc_info = retry_state.outcome.exception()
        if isinstance(exc_info, httpx.HTTPStatusError):
            response = exc_info.response
            error_message = exc_info.response.text
    else:
        response = retry_state.outcome.result()

    LOG.debug(
        "http request ends",
        method=method,
        url=url,
        gh_owner=gh_owner,
        error_message=error_message,
        attempts=retry_state.attempt_number,
        seconds_since_start=retry_state.seconds_since_start,
        idle_sinde_start=retry_state.idle_for,
        response=response,
        exc_info=exc_info,
    )


connectivity_issue_retry = tenacity.retry(
    reraise=True,
    retry=tenacity.retry_if_exception_type(  # type: ignore[no-untyped-call]
        (RequestError, HTTPServerSideError, HTTPTooManyRequests)
    ),
    wait=tenacity.wait_combine(  # type: ignore[no-untyped-call]
        wait_retry_after_header, tenacity.wait_exponential(multiplier=0.2)  # type: ignore[no-untyped-call]
    ),
    stop=tenacity.stop_after_attempt(5),  # type: ignore[no-untyped-call]
    before=before_log,
    after=after_log,
)


def raise_for_status(resp: httpx.Response) -> None:
    if httpx.codes.is_client_error(resp.status_code):
        error_type = "Client Error"
        exc_class = STATUS_CODE_TO_EXC.get(resp.status_code, HTTPClientSideError)
    elif httpx.codes.is_server_error(resp.status_code):
        error_type = "Server Error"
        exc_class = STATUS_CODE_TO_EXC.get(resp.status_code, HTTPServerSideError)
    else:
        return

    try:
        details = resp.json().get("message")
    except json.JSONDecodeError:
        details = None

    if details is None:
        details = resp.text if resp.text else "<empty-response>"

    message = f"{resp.status_code} {error_type}: {resp.reason_phrase} for url `{resp.url}`\nDetails: {details}"
    raise exc_class(message, request=resp.request, response=resp)


class AsyncClient(httpx.AsyncClient):
    @connectivity_issue_retry
    async def request(self, *args: typing.Any, **kwargs: typing.Any) -> httpx.Response:
        resp = await super().request(*args, **kwargs)
        raise_for_status(resp)
        return resp
