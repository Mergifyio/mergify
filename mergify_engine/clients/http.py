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


import json

import httpcore
import httpx
import urllib3

from mergify_engine import logs


LOG = logs.getLogger(__name__)

RETRY = urllib3.Retry(
    total=None,
    redirect=3,
    connect=5,
    read=5,
    status=5,
    backoff_factor=0.2,
    status_forcelist=list(range(500, 599)) + [429],
    method_whitelist=[
        "HEAD",
        "TRACE",
        "GET",
        "PUT",
        "OPTIONS",
        "DELETE",
        "POST",
        "PATCH",
    ],
    raise_on_status=False,
)
DEFAULT_CLIENT_OPTIONS = {
    "headers": {
        "Accept": "application/vnd.github.machine-man-preview+json",
        "User-Agent": "Mergify/Python",
    },
    "trust_env": False,
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


STATUS_CODE_TO_EXC = {404: HTTPNotFound}


class ClientMixin:
    @staticmethod
    def raise_for_status(resp):
        message = "{0.status_code} {error_type}: {0.reason_phrase} for url: {0.url}"
        if httpx.StatusCode.is_client_error(resp.status_code):
            message = message.format(resp, error_type="Client Error")
            try:
                gh_message = resp.json().get("message")
            except json.JSONDecodeError:
                gh_message = None
            if gh_message:
                message += f"\nGitHub details: {gh_message}"
            elif resp.text:
                message += f"\nDetails: {resp.text}"
            exc_class = STATUS_CODE_TO_EXC.get(resp.status_code, HTTPClientSideError)
            raise exc_class(message, response=resp)
        elif httpx.StatusCode.is_server_error(resp.status_code):
            message = message.format(resp, error_type="Server Error")
            if resp.text:
                message += f"\nDetails: {resp.text}"
            raise HTTPServerSideError(message, response=resp)


class Client(httpx.Client, ClientMixin):
    def __init__(self, *args, **kwargs):
        # TODO(sileht): Due to our retries config, we have to use URLLIB3 transport
        # instead of the httpx default. It doesn't looks like httpx/httpcore will ever
        # retry themself
        #
        # So, the plan is to use tenacity around `request()` to mimic urllib3 retry,
        # so Sync and Async client will share the exact same code for retrying

        # https://github.com/encode/httpx/blob/master/httpx/_transports/urllib3.py#L100
        transport = httpx.URLLib3Transport()

        real_urlopen = transport.pool.urlopen

        def _mergify_patched_urlopen(*args, **kwargs):
            kwargs["retries"] = RETRY
            return real_urlopen(*args, **kwargs)

        transport.pool.urlopen = _mergify_patched_urlopen

        kwargs["transport"] = transport
        kwargs["trust_env"] = False
        super().__init__(*args, **kwargs)

    def request(self, method, url, *args, **kwargs):
        LOG.debug("http request start", method=method, url=url)
        resp = super().request(method, url, *args, **kwargs)
        LOG.debug("http request end", method=method, url=url)
        self.raise_for_status(resp)
        return resp


class AsyncClient(httpx.AsyncClient, ClientMixin):
    # TODO(sileht): Handle retries like we do with urllib3

    async def request(self, method, url, *args, **kwargs):
        LOG.debug("http request start", method=method, url=url)
        resp = await super().request(method, url, *args, **kwargs)
        LOG.debug("http request end", method=method, url=url)
        self.raise_for_status(resp)
        return resp
