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

from unittest import mock

from mergify_engine import check_api
from mergify_engine import context


def test_summary_synchronization_cache():
    client = mock.MagicMock()
    client.auth.get_access_token.return_value = "<token>"

    ctxt = context.Context(
        client,
        {
            "number": 6,
            "merged": True,
            "state": "closed",
            "html_url": "<html_url>",
            "base": {
                "sha": "sha",
                "user": {"login": "user"},
                "ref": "ref",
                "repo": {
                    "id": 456,
                    "full_name": "user/ref",
                    "name": "name",
                    "private": False,
                    "owner": {"id": 1},
                },
            },
            "head": {
                "sha": "old-sha-one",
                "ref": "fork",
                "repo": {
                    "id": 123,
                    "full_name": "fork/other",
                    "name": "other",
                    "private": False,
                    "owner": {"id": 2},
                },
            },
            "user": {"login": "user"},
            "merged_by": None,
            "merged_at": None,
            "mergeable_state": "clean",
        },
        {},
    )
    assert ctxt.get_cached_last_summary_head_sha() is None
    ctxt.set_summary_check(check_api.Result(check_api.Conclusion.SUCCESS, "foo", "bar"))

    assert ctxt.get_cached_last_summary_head_sha() == "old-sha-one"
    ctxt.clear_cached_last_summary_head_sha()

    assert ctxt.get_cached_last_summary_head_sha() is None
