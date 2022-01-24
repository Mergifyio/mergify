from unittest import mock

import pytest

from mergify_engine import config
from mergify_engine import github_types
from mergify_engine.clients import http
from mergify_engine.dashboard import application


async def test_init(redis_cache):
    application.Application(
        redis_cache,
        0,
        "app name",
        "api_access_key",
        "api_secret_key",
        {
            "id": github_types.GitHubAccountIdType(12345),
            "login": github_types.GitHubLogin("login"),
        },
    )

    application.Application(
        redis_cache,
        0,
        "app name",
        "api_access_key",
        "api_secret_key",
        None,
    )


async def test_save_apikey_scoped(redis_cache):
    api_access_key = "a" * 32
    api_secret_key = "s" * 32
    account_id = github_types.GitHubAccountIdType(12345)
    account_login = github_types.GitHubLogin("login")
    app = application.Application(
        redis_cache,
        0,
        "app name",
        api_access_key,
        api_secret_key,
        {"id": account_id, "login": account_login},
    )

    await app.save_to_cache()
    rapp = await application.Application._retrieve_from_cache(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    assert app == rapp

    # wrong scope
    app = await application.Application._retrieve_from_cache(
        redis_cache,
        api_access_key,
        api_secret_key,
        github_types.GitHubLogin("another login"),
    )
    assert app is None
    app = await application.Application._retrieve_from_cache(
        redis_cache,
        api_access_key,
        api_secret_key,
        None,
    )
    assert app is None


async def test_save_apikey_unscoped(redis_cache):
    api_access_key = "a" * 32
    api_secret_key = "s" * 32
    app = application.Application(
        redis_cache, 0, "app name", api_access_key, api_secret_key, None
    )

    await app.save_to_cache()
    rapp = await application.Application._retrieve_from_cache(
        redis_cache,
        api_access_key,
        api_secret_key,
        None,
    )
    assert app == rapp

    # wrong scope
    app = await application.Application._retrieve_from_cache(
        redis_cache,
        api_access_key,
        api_secret_key,
        github_types.GitHubLogin("login"),
    )
    assert app is None


async def test_update_apikey(redis_cache):
    api_access_key = "a" * 32
    api_secret_key = "s" * 32
    account_id = github_types.GitHubAccountIdType(12345)
    account_login = github_types.GitHubLogin("login")

    app = application.Application(
        redis_cache,
        0,
        "app name",
        api_access_key,
        api_secret_key,
        {"id": account_id, "login": account_login},
    )

    await app.save_to_cache()
    rapp = await application.Application._retrieve_from_cache(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    assert app == rapp

    await application.Application.update(
        redis_cache,
        api_access_key,
        application.ApplicationDashboardJSON(
            {
                "id": 1,
                "name": "new name",
                "github_account": {
                    "id": 424242,
                    "login": "login",
                    "type": "User",
                    "avatar_url": "",
                },
            }
        ),
    )
    rapp = await application.Application._retrieve_from_cache(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    expected_app = application.Application(
        redis_cache,
        1,
        "new name",
        api_access_key,
        api_secret_key,
        {"id": 424242, "login": "login"},
        ttl=application.Application.RETENTION_SECONDS,
    )

    assert expected_app == rapp


@mock.patch.object(application.Application, "_retrieve_from_db")
async def test_application_db_unavailable(retrieve_from_db_mock, redis_cache):
    api_access_key = "a" * 32
    api_secret_key = "s" * 32
    account_id = github_types.GitHubAccountIdType(12345)
    account_login = github_types.GitHubLogin("login")
    app = application.Application(
        redis_cache,
        0,
        "app name",
        api_access_key,
        api_secret_key,
        {"id": account_id, "login": account_login},
    )
    retrieve_from_db_mock.return_value = app

    # no cache, no db -> reraise
    retrieve_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    with pytest.raises(http.HTTPServiceUnavailable):
        await application.Application.get(
            redis_cache, api_access_key, api_secret_key, account_login
        )
        retrieve_from_db_mock.assert_called_once()

    # no cache, bapp db -> got db app
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = None
    rapp = await application.Application.get(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    assert app == rapp
    retrieve_from_db_mock.assert_called_once()

    # cache not expired and not db -> got cached  app
    retrieve_from_db_mock.reset_mock()
    rapp = await application.Application.get(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    app.ttl = 259200
    assert rapp == app
    retrieve_from_db_mock.assert_not_called()

    # cache expired and not db -> got cached  app
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    await redis_cache.expire(f"api-key-cache~{api_access_key}~{account_login}", 7200)
    rapp = await application.Application.get(
        redis_cache, api_access_key, api_secret_key, account_login
    )
    app.ttl = 7200
    assert rapp == app
    retrieve_from_db_mock.assert_called_once()

    # cache expired and unexpected db issue -> reraise
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = Exception("WTF")
    await redis_cache.expire(f"api-key-cache~{api_access_key}~{account_login}", 7200)
    with pytest.raises(Exception):
        await application.Application.get(
            redis_cache, api_access_key, api_secret_key, account_login
        )
    retrieve_from_db_mock.assert_called_once()


async def test_unknown_app(redis_cache):
    app = await application.Application._retrieve_from_cache(
        redis_cache, "whatever", "secret", None
    )
    assert app is None


async def test_application_tokens_via_env(monkeypatch, redis_cache):
    api_access_key1 = "1" * 32
    api_secret_key1 = "1" * 32
    account_id1 = github_types.GitHubAccountIdType(12345)
    account_login1 = github_types.GitHubLogin("login1")

    api_access_key2 = "2" * 32
    api_secret_key2 = "2" * 32
    account_id2 = github_types.GitHubAccountIdType(67891)
    account_login2 = github_types.GitHubLogin("login2")

    with pytest.raises(application.ApplicationUserNotFound):
        await application.ApplicationOnPremise.get(
            redis_cache, api_access_key1, api_secret_key1, None
        )

    with pytest.raises(application.ApplicationUserNotFound):
        await application.ApplicationOnPremise.get(
            redis_cache, api_access_key2, api_secret_key2, None
        )

    monkeypatch.setattr(
        config,
        "APPLICATION_APIKEYS",
        config.ApplicationAPIKeys(
            f"{api_access_key1}{api_secret_key1}:{account_id1}:{account_login1},{api_access_key2}{api_secret_key2}:{account_id2}:{account_login2}"
        ),
    )

    app = await application.ApplicationOnPremise.get(
        redis_cache, api_access_key1, api_secret_key1, account_login1
    )
    assert app.account_scope["id"] == account_id1

    app = await application.ApplicationOnPremise.get(
        redis_cache, api_access_key2, api_secret_key2, account_login2
    )
    assert app.account_scope["id"] == account_id2

    app = await application.ApplicationOnPremise.get(
        redis_cache, api_access_key1, api_secret_key1, None
    )
    assert app.account_scope["id"] == account_id1

    app = await application.ApplicationOnPremise.get(
        redis_cache, api_access_key2, api_secret_key2, None
    )
    assert app.account_scope["id"] == account_id2

    # wrong scope
    with pytest.raises(application.ApplicationUserNotFound):
        await application.ApplicationOnPremise.get(
            redis_cache, api_access_key1, api_secret_key1, account_id2
        )
    with pytest.raises(application.ApplicationUserNotFound):
        await application.ApplicationOnPremise.get(
            redis_cache, api_access_key2, api_secret_key2, account_id1
        )
