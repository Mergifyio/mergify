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


from mergify_engine import RETRY


DEFAULT_CLIENT_OPTIONS = {
    "headers": {
        "Accept": "application/vnd.github.machine-man-preview+json",
        "User-Agent": "Mergify/Python",
    },
    "trust_env": False,
}


class HttpxRetriesMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # httpx doesn't support retries yet, but the sync client uses urllib3 like request
        # https://github.com/encode/httpx/blob/master/httpx/_dispatch/urllib3.py#L105

        real_url_open = self.dispatch.pool.url_open

        def _mergify_patched_url_open(*args, **kwargs):
            kwargs["retries"] = RETRY
            return real_url_open(*args, **kwargs)

        self.dispatch.pool.url_open = _mergify_patched_url_open
