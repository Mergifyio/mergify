# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2022 Mergify SAS
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

import datetime
import email.utils
import sys
import typing

import daiquiri
import httpx
from httpx import _types as httpx_types
import tenacity
import tenacity.wait

from mergify_engine import date
from mergify_engine import service


LOG = daiquiri.getLogger(__name__)

PYTHON_VERSION = (
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
)
HTTPX_VERSION = httpx.__version__

DEFAULT_HEADERS = httpx.Headers(
    {
        "User-Agent": f"mergify-engine/{service.VERSION} python/{PYTHON_VERSION} httpx/{HTTPX_VERSION}"
    }
)

DEFAULT_TIMEOUT = httpx.Timeout(5.0, read=10.0)

HTTPStatusError = httpx.HTTPStatusError
RequestError = httpx.RequestError


class HTTPServerSideError(httpx.HTTPStatusError):
    @property
    def message(self) -> str:
        return self.response.text

    @property
    def status_code(self) -> int:
        return self.response.status_code


class HTTPClientSideError(httpx.HTTPStatusError):
    @property
    def message(self) -> str:
        # TODO(sileht): do something with errors and documentation_url when present
        # https://developer.github.com/v3/#client-errors
        response = self.response.json()
        message = response.get("message", "No error message provided by GitHub")

        if "errors" in response:
            if "message" in response["errors"][0]:
                message = response["errors"][0]["message"]
            elif isinstance(response["errors"][0], str):
                message = response["errors"][0]

        return typing.cast(str, message)

    @property
    def status_code(self) -> int:
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


def parse_date(value: str) -> typing.Optional[datetime.datetime]:
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)

    return dt


class wait_retry_after_header(tenacity.wait.wait_base):
    def __call__(self, retry_state: tenacity.RetryCallState) -> float:
        if retry_state.outcome is None:
            return 0

        exc = retry_state.outcome.exception()
        if exc is None or not isinstance(exc, HTTPStatusError):
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


def extract_organization_login(client: httpx.AsyncClient) -> typing.Optional[str]:
    if client.auth and hasattr(client.auth, "_owner_login"):
        return client.auth._owner_login  # type: ignore[attr-defined,no-any-return]
    return None


def before_log(retry_state: tenacity.RetryCallState) -> None:
    client = retry_state.args[0]
    method = retry_state.args[1]
    gh_owner = extract_organization_login(client)
    url = retry_state.args[2]
    LOG.debug(
        "http request starts",
        method=method,
        url=url,
        gh_owner=gh_owner,
        attempts=retry_state.attempt_number,
    )


def after_log(retry_state: tenacity.RetryCallState) -> None:
    client = retry_state.args[0]
    method = retry_state.args[1]
    url = retry_state.args[2]
    gh_owner = extract_organization_login(client)
    error_message = None
    response = None
    exc_info = None
    if retry_state.outcome:
        if retry_state.outcome.failed:
            exc_info = retry_state.outcome.exception()
            if isinstance(exc_info, httpx.HTTPStatusError):
                response = exc_info.response
                error_message = exc_info.response.text
        else:
            response = retry_state.outcome.result()
    else:
        response = None

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
    retry=tenacity.retry_if_exception_type(
        (RequestError, HTTPServerSideError, HTTPTooManyRequests)
    ),
    wait=tenacity.wait_combine(
        wait_retry_after_header(), tenacity.wait_exponential(multiplier=0.2)
    ),
    stop=tenacity.stop_after_attempt(5),
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

    details = resp.text if resp.text else "<empty-response>"
    message = f"{resp.status_code} {error_type}: {resp.reason_phrase} for url `{resp.url}`\nDetails: {details}"
    raise exc_class(message, request=resp.request, response=resp)


class AsyncClient(httpx.AsyncClient):
    def __init__(
        self,
        auth: typing.Optional[httpx_types.AuthTypes] = None,
        headers: typing.Optional[httpx_types.HeaderTypes] = None,
        timeout: httpx_types.TimeoutTypes = DEFAULT_TIMEOUT,
        base_url: httpx_types.URLTypes = "",
    ) -> None:
        final_headers = DEFAULT_HEADERS.copy()
        if headers is not None:
            final_headers.update(headers)
        super().__init__(
            auth=auth,
            base_url=base_url,
            headers=final_headers,
            timeout=timeout,
            follow_redirects=True,
        )

    @connectivity_issue_retry
    async def request(self, *args: typing.Any, **kwargs: typing.Any) -> httpx.Response:
        resp = await super().request(*args, **kwargs)
        raise_for_status(resp)
        return resp
