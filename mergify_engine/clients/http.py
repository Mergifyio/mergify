# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import json

import daiquiri
import httpcore
import httpx
import tenacity
from werkzeug.http import parse_date


LOG = daiquiri.getLogger(__name__)

DEFAULT_CLIENT_OPTIONS = {
    "headers": {
        "Accept": "application/vnd.github.machine-man-preview+json",
        "User-Agent": "Mergify/Python",
    },
}

HTTPError = httpx.HTTPError

# WARNING(sileht): httpx completly mess up exception handling of version 0.13.1, most
# of them doesn't inherit anymore from HTTPError, they are aware of that and plan to
# change/break it again before the 1.0 release
# cf: https://github.com/encode/httpx/issues/949
ConnectionErrors = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.ProxyError,
)


class HTTPServerSideError(httpx.HTTPError):
    @property
    def message(self):
        return self.response.text

    @property
    def status_code(self):
        return self.response.status_code


class HTTPClientSideError(httpx.HTTPError):
    @property
    def message(self):
        # TODO(sileht): do something with errors and documentation_url when present
        # https://developer.github.com/v3/#client-errors
        return self.response.json()["message"]

    @property
    def status_code(self):
        return self.response.status_code


class HTTPNotFound(HTTPClientSideError):
    pass


class HTTPTooManyRequests(HTTPClientSideError):
    pass


class HTTPServiceUnavailable(HTTPServerSideError):
    pass


STATUS_CODE_TO_EXC = {
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
    return max(0, (d - datetime.datetime.utcnow()).total_seconds())


connectivity_issue_retry = tenacity.retry(
    reraise=True,
    retry=tenacity.retry_if_exception_type(
        ConnectionErrors + (HTTPServerSideError, HTTPTooManyRequests)
    ),
    wait=tenacity.wait_combine(
        wait_retry_after_header, tenacity.wait_exponential(multiplier=0.2)
    ),
    stop=tenacity.stop_after_attempt(5),
)


def raise_for_status(resp):
    if httpx.StatusCode.is_client_error(resp.status_code):
        error_type = "Client Error"
        default_exception = HTTPClientSideError
    elif httpx.StatusCode.is_server_error(resp.status_code):
        error_type = "Server Error"
        default_exception = HTTPServerSideError
    else:
        return

    try:
        details = resp.json().get("message")
    except json.JSONDecodeError:
        details = None

    if details is None:
        details = resp.text if resp.text else "<empty-response>"

    message = f"{resp.status_code} {error_type}: {resp.reason_phrase} for url `{resp.url}`\nDetails: {details}"
    exc_class = STATUS_CODE_TO_EXC.get(resp.status_code, default_exception)
    raise exc_class(message, response=resp)


class Client(httpx.Client):
    @connectivity_issue_retry
    def request(self, method, url, *args, **kwargs):
        LOG.debug("http request start", method=method, url=url)
        resp = super().request(method, url, *args, **kwargs)
        LOG.debug("http request end", method=method, url=url)
        raise_for_status(resp)
        return resp


class AsyncClient(httpx.AsyncClient):
    @connectivity_issue_retry
    async def request(self, method, url, *args, **kwargs):
        LOG.debug("http request start", method=method, url=url)
        resp = await super().request(method, url, *args, **kwargs)
        LOG.debug("http request end", method=method, url=url)
        raise_for_status(resp)
        return resp
