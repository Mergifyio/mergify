# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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

import pytest

from mergify_engine import debug
from mergify_engine import github_types


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mergifyio",
        "https://github.com//mergifyio",
        "https://github.com//mergifyio//",
    ],
)
def test_url_parser_with_owner_ok(url: str) -> None:
    assert debug._url_parser(url) == ("mergifyio", None, None)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mergifyio/mergify-engine",
        "https://github.com//mergifyio//mergify-engine//",
    ],
)
def test_url_parser_with_repo_ok(url: str) -> None:
    assert debug._url_parser(url) == ("mergifyio", "mergify-engine", None)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mergifyio/mergify-engine/pull/123",
        "https://github.com/mergifyio/mergify-engine/pull/123#",
        "https://github.com/mergifyio/mergify-engine/pull/123#",
        "https://github.com/mergifyio/mergify-engine/pull/123#42",
        "https://github.com/mergifyio/mergify-engine/pull/123?foo=345",
        "https://github.com/mergifyio/mergify-engine/pull/123?foo=456&bar=567c",
        "https://github.com/mergifyio/mergify-engine/pull/123?foo",
        "https://github.com//mergifyio/mergify-engine/pull/123",
        "https://github.com/mergifyio//mergify-engine/pull/123",
        "https://github.com//mergifyio/mergify-engine//pull/123",
        "https://github.com//mergifyio/mergify-engine/pull//123",
    ],
)
def test_url_parser_with_pr_ok(url: str) -> None:
    assert debug._url_parser(url) == (
        github_types.GitHubLogin("mergifyio"),
        github_types.GitHubRepositoryName("mergify-engine"),
        github_types.GitHubPullRequestNumber(123),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/",
        "https://github.com/mergifyio/mergify-engine/123",
        "https://github.com/mergifyio/mergify-engine/foobar/pull/123",
        "https://github.com/mergifyio/mergify-engine/foobar/pull/123/foobar/pull/123/",
    ],
)
def test_url_parser_fail(url: str) -> None:
    with pytest.raises(ValueError):
        debug._url_parser(url)
