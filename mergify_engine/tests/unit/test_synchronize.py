# -*- encoding: utf-8 -*-
#
# Copyright © 2020—2021 Mergify SAS
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
from mergify_engine import github_types
from mergify_engine.tests.unit import conftest


async def test_summary_synchronization_cache(
    context_getter: conftest.ContextGetterFixture,
) -> None:
    async def items(*args, **kwargs):
        if False:
            yield
        return

    async def post_check(*args, **kwargs):
        return mock.Mock(
            status_code=200,
            json=mock.Mock(
                return_value=github_types.GitHubCheckRun(
                    {
                        "head_sha": "ce587453ced02b1526dfb4cb910479d431683101",
                        "details_url": "https://example.com",
                        "status": "completed",
                        "conclusion": "neutral",
                        "name": "neutral",
                        "id": 1236,
                        "app": {
                            "id": 1234,
                            "name": "CI",
                            "owner": {
                                "type": "User",
                                "id": 1234,
                                "login": "goo",
                                "avatar_url": "https://example.com",
                            },
                        },
                        "external_id": "",
                        "pull_requests": [],
                        "before": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                        "after": "4eef79d038b0327a5e035fd65059e556a55c6aa4",
                        "started_at": "",
                        "completed_at": "",
                        "html_url": "https://example.com",
                        "check_suite": {"id": 1234},
                        "output": {
                            "summary": "",
                            "title": "It runs!",
                            "text": "",
                            "annotations": [],
                            "annotations_count": 0,
                            "annotations_url": "https://example.com",
                        },
                    }
                )
            ),
        )

    client = mock.AsyncMock()
    client.auth.get_access_token.return_value = "<token>"
    client.items = items
    client.post.side_effect = post_check

    ctxt = await context_getter(github_types.GitHubPullRequestNumber(6))
    ctxt.repository.installation.client = client
    assert await ctxt.get_cached_last_summary_head_sha() is None
    await ctxt.set_summary_check(
        check_api.Result(check_api.Conclusion.SUCCESS, "foo", "bar")
    )

    assert await ctxt.get_cached_last_summary_head_sha() == "the-head-sha"
    await ctxt.clear_cached_last_summary_head_sha()

    assert await ctxt.get_cached_last_summary_head_sha() is None
