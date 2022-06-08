# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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

from mergify_engine import config


def test_legacy_api_url(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://api.github.com"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://api.github.com/graphql"

    monkeypatch.setenv(
        "MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/api/v3/"
    )
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://onprem.example.com/api/v3"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://onprem.example.com/api/graphql"

    monkeypatch.setenv(
        "MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/api/v3"
    )
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://onprem.example.com/api/v3"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://onprem.example.com/api/graphql"

    # Not a valid GHES url, just ignore it...
    monkeypatch.setenv("MERGIFYENGINE_GITHUB_API_URL", "https://onprem.example.com/")
    conf = config.load()
    assert conf["GITHUB_REST_API_URL"] == "https://api.github.com"
    assert conf["GITHUB_GRAPHQL_API_URL"] == "https://api.github.com/graphql"


def test_redis_onpremise_legacy(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MERGIFYENGINE_DEFAULT_REDIS_URL")
    monkeypatch.setenv("MERGIFYENGINE_STORAGE_URL", "rediss://redis.example.com:1234")
    conf = config.load()
    assert conf["STREAM_URL"] == "rediss://redis.example.com:1234"
    assert conf["QUEUE_URL"] == "rediss://redis.example.com:1234"
    assert conf["LEGACY_CACHE_URL"] == "rediss://redis.example.com:1234"
    assert conf["TEAM_MEMBERS_CACHE_URL"] == "rediss://redis.example.com:1234?db=5"
    assert conf["TEAM_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=6"
    assert conf["USER_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=7"


def test_redis_saas_current(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MERGIFYENGINE_DEFAULT_REDIS_URL", "rediss://redis.example.com:1234"
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_STORAGE_URL",
        "rediss://redis-legacy-cache.example.com:1234?db=2",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_STREAM_URL",
        "rediss://redis-stream.example.com:1234?db=3",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_QUEUE_URL", "rediss://redis-queue.example.com:1234?db=4"
    )
    conf = config.load()
    assert (
        conf["LEGACY_CACHE_URL"] == "rediss://redis-legacy-cache.example.com:1234?db=2"
    )
    assert conf["STREAM_URL"] == "rediss://redis-stream.example.com:1234?db=3"
    assert conf["QUEUE_URL"] == "rediss://redis-queue.example.com:1234?db=4"
    assert conf["TEAM_MEMBERS_CACHE_URL"] == "rediss://redis.example.com:1234?db=5"
    assert conf["TEAM_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=6"
    assert conf["USER_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=7"


def test_redis_default(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MERGIFYENGINE_DEFAULT_REDIS_URL", "rediss://redis.example.com:1234"
    )
    conf = config.load()
    assert conf["LEGACY_CACHE_URL"] == "rediss://redis.example.com:1234?db=2"
    assert conf["STREAM_URL"] == "rediss://redis.example.com:1234?db=3"
    assert conf["QUEUE_URL"] == "rediss://redis.example.com:1234?db=4"
    assert conf["TEAM_MEMBERS_CACHE_URL"] == "rediss://redis.example.com:1234?db=5"
    assert conf["TEAM_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=6"
    assert conf["USER_PERMISSIONS_CACHE_URL"] == "rediss://redis.example.com:1234?db=7"


def test_redis_all_set(
    original_environment_variables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MERGIFYENGINE_DEFAULT_REDIS_URL",
        "rediss://redis-default.example.com:1234",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_LEGACY_CACHE_URL",
        "rediss://redis-legacy-cache.example.com:1234",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_STREAM_URL",
        "rediss://redis-stream.example.com:1234",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_QUEUE_URL", "rediss://redis-queue.example.com:1234"
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_TEAM_MEMBERS_CACHE_URL",
        "rediss://redis-team-members.example.com:1234",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_TEAM_PERMISSIONS_CACHE_URL",
        "rediss://redis-team-perm.example.com:1234",
    )
    monkeypatch.setenv(
        "MERGIFYENGINE_USER_PERMISSIONS_CACHE_URL",
        "rediss://redis-user-perm.example.com:1234",
    )
    conf = config.load()
    assert conf["LEGACY_CACHE_URL"] == "rediss://redis-legacy-cache.example.com:1234"
    assert conf["STREAM_URL"] == "rediss://redis-stream.example.com:1234"
    assert conf["QUEUE_URL"] == "rediss://redis-queue.example.com:1234"
    assert (
        conf["TEAM_MEMBERS_CACHE_URL"] == "rediss://redis-team-members.example.com:1234"
    )
    assert (
        conf["TEAM_PERMISSIONS_CACHE_URL"]
        == "rediss://redis-team-perm.example.com:1234"
    )
    assert (
        conf["USER_PERMISSIONS_CACHE_URL"]
        == "rediss://redis-user-perm.example.com:1234"
    )
