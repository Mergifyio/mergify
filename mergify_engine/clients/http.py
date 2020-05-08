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


httpx.HTTPClientSideError = HTTPClientSideError
httpx.HTTPNotFound = HTTPNotFound

STATUS_CODE_TO_EXC = {404: HTTPNotFound}


class Client(httpx.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # httpx doesn't support retries yet, but the sync client uses urllib3 like request
        # https://github.com/encode/httpx/blob/master/httpx/_dispatch/urllib3.py#L105

        real_urlopen = self.dispatch.pool.urlopen

        def _mergify_patched_urlopen(*args, **kwargs):
            kwargs["retries"] = RETRY
            return real_urlopen(*args, **kwargs)

        self.dispatch.pool.urlopen = _mergify_patched_urlopen

    def request(self, method, url, *args, **kwargs):
        LOG.debug("http request start", method=method, url=url)
        try:
            r = super().request(method, url, *args, **kwargs)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            if e.response and 400 <= e.response.status_code < 500:
                exc_class = STATUS_CODE_TO_EXC.get(
                    e.response.status_code, HTTPClientSideError
                )
                message = e.args[0]
                gh_message = e.response.json().get("message")
                if gh_message:
                    message = f"{message}\nGitHub details: {gh_message}"
                raise exc_class(
                    message, *e.args[1:], request=e.request, response=e.response,
                )
            raise
        finally:
            LOG.debug("http request end", method=method, url=url)
