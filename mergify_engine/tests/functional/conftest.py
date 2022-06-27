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
import asyncio
import datetime
import json
import os
import shutil
import typing
from unittest import mock

import httpx
import pytest
import vcr
import vcr.stubs.urllib3_stubs

from mergify_engine import config
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.dashboard import application as application_mod
from mergify_engine.dashboard import subscription
from mergify_engine.dashboard import user_tokens as user_tokens_mod
from mergify_engine.web import root as web_root


RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = "zfixtures/cassettes"


class RecordConfigType(typing.TypedDict):
    integration_id: int
    app_user_id: github_types.GitHubAccountIdType
    app_user_login: github_types.GitHubLogin
    organization_id: github_types.GitHubAccountIdType
    organization_name: github_types.GitHubLogin
    repository_id: github_types.GitHubRepositoryIdType
    repository_name: github_types.GitHubRepositoryName
    branch_prefix: str


@pytest.fixture
async def mergify_web_client() -> typing.AsyncGenerator[httpx.AsyncClient, None]:
    await web_root.startup()
    client = httpx.AsyncClient(app=web_root.app, base_url="http://localhost")
    try:
        yield client
    finally:
        await client.aclose()
        await web_root.shutdown()


class DashboardFixture(typing.NamedTuple):
    api_key_admin: str
    subscription: subscription.Subscription
    user_tokens: user_tokens_mod.UserTokens


@pytest.fixture
async def dashboard(
    redis_cache: redis_utils.RedisCache, request: pytest.FixtureRequest
) -> DashboardFixture:
    is_unittest_class = request.cls is not None
    subscription_active = False
    marker = request.node.get_closest_marker("subscription")
    if marker:
        subscription_active = marker.args[0]
    elif is_unittest_class:
        subscription_active = request.cls.SUBSCRIPTION_ACTIVE

    api_key_admin = "a" * 64

    sub = subscription.Subscription(
        redis_cache,
        config.TESTING_ORGANIZATION_ID,
        "You're not nice",
        frozenset(
            getattr(subscription.Features, f) for f in subscription.Features.__members__
        )
        if subscription_active
        else frozenset(
            # EVENTLOGS requires subscription, but not for tests
            [
                subscription.Features.PUBLIC_REPOSITORY,
                subscription.Features.EVENTLOGS_SHORT,
                subscription.Features.EVENTLOGS_LONG,
            ]
        ),
    )
    await sub._save_subscription_to_cache()
    user_tokens = user_tokens_mod.UserTokens(
        redis_cache,
        config.TESTING_ORGANIZATION_ID,
        [
            {
                "id": github_types.GitHubAccountIdType(config.ORG_USER_ID),
                "login": github_types.GitHubLogin("mergify-test4"),
                "oauth_access_token": config.ORG_USER_PERSONAL_TOKEN,
                "name": None,
                "email": None,
            },
        ],
    )
    await typing.cast(user_tokens_mod.UserTokensSaas, user_tokens).save_to_cache()

    real_get_subscription = subscription.Subscription.get_subscription

    async def fake_retrieve_subscription_from_db(redis_cache, owner_id):
        if owner_id == config.TESTING_ORGANIZATION_ID:
            return sub
        return subscription.Subscription(
            redis_cache,
            owner_id,
            "We're just testing",
            set(subscription.Features.PUBLIC_REPOSITORY),
        )

    async def fake_subscription(redis_cache, owner_id):
        if owner_id == config.TESTING_ORGANIZATION_ID:
            return await real_get_subscription(redis_cache, owner_id)
        return subscription.Subscription(
            redis_cache,
            owner_id,
            "We're just testing",
            set(subscription.Features.PUBLIC_REPOSITORY),
        )

    patcher = mock.patch(
        "mergify_engine.dashboard.subscription.Subscription._retrieve_subscription_from_db",
        side_effect=fake_retrieve_subscription_from_db,
    )
    patcher.start()
    request.addfinalizer(patcher.stop)

    patcher = mock.patch(
        "mergify_engine.dashboard.subscription.Subscription.get_subscription",
        side_effect=fake_subscription,
    )
    patcher.start()
    request.addfinalizer(patcher.stop)

    async def fake_retrieve_user_tokens_from_db(redis_cache, owner_id):
        if owner_id == config.TESTING_ORGANIZATION_ID:
            return user_tokens
        return user_tokens_mod.UserTokens(redis_cache, owner_id, {})

    real_get_user_tokens = user_tokens_mod.UserTokens.get

    async def fake_user_tokens(redis_cache, owner_id):
        if owner_id == config.TESTING_ORGANIZATION_ID:
            return await real_get_user_tokens(redis_cache, owner_id)
        return user_tokens_mod.UserTokens(redis_cache, owner_id, {})

    patcher = mock.patch(
        "mergify_engine.dashboard.user_tokens.UserTokensSaas._retrieve_from_db",
        side_effect=fake_retrieve_user_tokens_from_db,
    )
    patcher.start()
    request.addfinalizer(patcher.stop)

    patcher = mock.patch(
        "mergify_engine.dashboard.user_tokens.UserTokensSaas.get",
        side_effect=fake_user_tokens,
    )
    patcher.start()
    request.addfinalizer(patcher.stop)

    async def fake_application_get(
        redis_cache, api_access_key, api_secret_key, account_scope
    ):
        if (
            api_access_key == api_key_admin[:32]
            and api_secret_key == api_key_admin[32:]
        ):
            return application_mod.Application(
                redis_cache,
                123,
                "testing application",
                api_access_key,
                api_secret_key,
                account_scope={
                    "id": config.TESTING_ORGANIZATION_ID,
                    "login": config.TESTING_ORGANIZATION_NAME,
                },
            )
        raise application_mod.ApplicationUserNotFound()

    patcher = mock.patch(
        "mergify_engine.dashboard.application.ApplicationSaas.get",
        side_effect=fake_application_get,
    )
    patcher.start()
    request.addfinalizer(patcher.stop)

    return DashboardFixture(
        api_key_admin,
        sub,
        user_tokens,
    )


def pyvcr_response_filter(response):
    for h in [
        "CF-Cache-Status",
        "CF-RAY",
        "Expect-CT",
        "Report-To",
        "NEL",
        "cf-request-id",
        "Via",
        "X-GitHub-Request-Id",
        "Date",
        "ETag",
        "X-RateLimit-Reset",
        "X-RateLimit-Used",
        "X-RateLimit-Resource",
        "X-RateLimit-Limit",
        "Via",
        "cookie",
        "Expires",
        "Fastly-Request-ID",
        "X-Timer",
        "X-Served-By",
        "Last-Modified",
        "X-RateLimit-Remaining",
        "X-Runtime-rack",
        "Access-Control-Allow-Origin",
        "Access-Control-Expose-Headers",
        "Cache-Control",
        "Content-Security-Policy",
        "Referrer-Policy",
        "Server",
        "Status",
        "Strict-Transport-Security",
        "Vary",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "X-XSS-Protection",
    ]:
        response["headers"].pop(h, None)
    return response


def pyvcr_request_filter(request):
    if request.method == "POST" and request.path.endswith("/access_tokens"):
        return None
    return request


class RecorderFixture(typing.NamedTuple):
    config: RecordConfigType
    vcr: vcr.VCR


@pytest.fixture(autouse=True)
async def recorder(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> typing.Optional[RecorderFixture]:
    is_unittest_class = request.cls is not None

    marker = request.node.get_closest_marker("recorder")
    if not is_unittest_class and marker is None:
        return None

    if is_unittest_class:
        cassette_library_dir = os.path.join(
            CASSETTE_LIBRARY_DIR_BASE,
            request.cls.__name__,
            request.node.name,
        )
    else:
        cassette_library_dir = os.path.join(
            CASSETTE_LIBRARY_DIR_BASE,
            request.node.module.__name__.replace(
                "mergify_engine.tests.functional.", ""
            ).replace(".", "/"),
            request.node.name,
        )

    # Recording stuffs
    if RECORD:
        if os.path.exists(cassette_library_dir):
            shutil.rmtree(cassette_library_dir)
        os.makedirs(cassette_library_dir)

    recorder = vcr.VCR(
        cassette_library_dir=cassette_library_dir,
        record_mode="all" if RECORD else "none",
        match_on=["method", "uri"],
        ignore_localhost=True,
        filter_headers=[
            ("Authorization", "<TOKEN>"),
            ("X-Hub-Signature", "<SIGNATURE>"),
            ("User-Agent", None),
            ("Accept-Encoding", None),
            ("Connection", None),
        ],
        before_record_response=pyvcr_response_filter,
        before_record_request=pyvcr_request_filter,
    )

    if RECORD:
        github.CachedToken.STORAGE = {}
    else:
        # Never expire token during replay
        patcher = mock.patch.object(
            github_app, "get_or_create_jwt", return_value="<TOKEN>"
        )
        patcher.start()
        request.addfinalizer(patcher.stop)
        patcher = mock.patch.object(
            github.GithubAppInstallationAuth,
            "get_access_token",
            return_value="<TOKEN>",
        )
        patcher.start()
        request.addfinalizer(patcher.stop)

    # Let's start recording
    cassette = recorder.use_cassette("http.json")
    cassette.__enter__()
    request.addfinalizer(cassette.__exit__)
    record_config_file = os.path.join(cassette_library_dir, "config.json")

    if RECORD:
        with open(record_config_file, "w") as f:
            f.write(
                json.dumps(
                    RecordConfigType(
                        {
                            "integration_id": config.INTEGRATION_ID,
                            "app_user_id": config.BOT_USER_ID,
                            "app_user_login": config.BOT_USER_LOGIN,
                            "organization_id": config.TESTING_ORGANIZATION_ID,
                            "organization_name": config.TESTING_ORGANIZATION_NAME,
                            "repository_id": config.TESTING_REPOSITORY_ID,
                            "repository_name": github_types.GitHubRepositoryName(
                                config.TESTING_REPOSITORY_NAME
                            ),
                            "branch_prefix": datetime.datetime.utcnow().strftime(
                                "%Y%m%d%H%M%S"
                            ),
                        }
                    )
                )
            )

    with open(record_config_file, "r") as f:
        recorder_config = typing.cast(RecordConfigType, json.loads(f.read()))
        monkeypatch.setattr(config, "INTEGRATION_ID", recorder_config["integration_id"])
        monkeypatch.setattr(config, "BOT_USER_ID", recorder_config["app_user_id"])
        monkeypatch.setattr(config, "BOT_USER_LOGIN", recorder_config["app_user_login"])
        return RecorderFixture(recorder_config, recorder)


@pytest.fixture
def unittest_glue(
    dashboard: DashboardFixture,
    mergify_web_client: httpx.AsyncClient,
    recorder: RecorderFixture,
    event_loop: asyncio.AbstractEventLoop,
    request: pytest.FixtureRequest,
) -> None:
    request.cls.api_key_admin = dashboard.api_key_admin
    request.cls.pytest_event_loop = event_loop
    request.cls.app = mergify_web_client
    request.cls.RECORD_CONFIG = recorder.config
    request.cls.cassette_library_dir = recorder.vcr.cassette_library_dir
    request.cls.subscription = dashboard.subscription
