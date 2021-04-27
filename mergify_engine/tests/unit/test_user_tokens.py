from unittest import mock

import pytest

from mergify_engine import config
from mergify_engine import user_tokens
from mergify_engine.clients import http


@pytest.mark.asyncio
async def test_init(redis_cache):
    user_tokens.UserTokens(redis_cache, 123, [])


@pytest.mark.parametrize(
    "users",
    (
        [],
        [{"login": "foo", "oauth_access_token": "bar", "name": None, "email": None}],
        [
            {"login": "foo", "oauth_access_token": "bar", "name": None, "email": None},
            {
                "login": "login",
                "oauth_access_token": "token",
                "name": None,
                "email": None,
            },
        ],
    ),
)
@pytest.mark.asyncio
async def test_save_ut(users, redis_cache):
    owner_id = 1234
    ut = user_tokens.UserTokens(
        redis_cache,
        owner_id,
        users,
    )

    await ut.save_to_cache()
    rut = await user_tokens.UserTokens._retrieve_from_cache(redis_cache, owner_id)
    assert ut == rut


@pytest.mark.asyncio
@mock.patch.object(user_tokens.UserTokens, "_retrieve_from_db")
async def test_user_tokens_db_unavailable(retrieve_from_db_mock, redis_cache):
    owner_id = 1234
    ut = user_tokens.UserTokens(redis_cache, owner_id, [])
    retrieve_from_db_mock.return_value = ut

    # no cache, no db -> reraise
    retrieve_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    with pytest.raises(http.HTTPServiceUnavailable):
        await user_tokens.UserTokens.get(redis_cache, owner_id)
        retrieve_from_db_mock.assert_called_once()

    # no cache, but db -> got db ut
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = None
    rut = await user_tokens.UserTokens.get(redis_cache, owner_id)
    assert ut == rut
    retrieve_from_db_mock.assert_called_once()

    # cache not expired and not db -> got cached  ut
    retrieve_from_db_mock.reset_mock()
    rut = await user_tokens.UserTokens.get(redis_cache, owner_id)
    ut.ttl = 259200
    assert rut == ut
    retrieve_from_db_mock.assert_not_called()

    # cache expired and not db -> got cached  ut
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    await redis_cache.expire(f"user-tokens-cache-owner-{owner_id}", 7200)
    rut = await user_tokens.UserTokens.get(redis_cache, owner_id)
    ut.ttl = 7200
    assert rut == ut
    retrieve_from_db_mock.assert_called_once()

    # cache expired and unexpected db issue -> reraise
    retrieve_from_db_mock.reset_mock()
    retrieve_from_db_mock.side_effect = Exception("WTF")
    await redis_cache.expire(f"user-tokens-cache-owner-{owner_id}", 7200)
    with pytest.raises(Exception):
        await user_tokens.UserTokens.get(redis_cache, owner_id)
    retrieve_from_db_mock.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_ut(redis_cache):
    tokens = await user_tokens.UserTokens._retrieve_from_cache(redis_cache, 98732189)
    assert tokens is None


@pytest.mark.asyncio
async def test_user_tokens_tokens_via_env(monkeypatch, redis_cache):
    ut = user_tokens.UserTokens(redis_cache, 123, [])

    assert ut.get_token_for("foo") is None
    assert ut.get_token_for("login") is None
    assert ut.get_token_for("nop") is None

    monkeypatch.setattr(
        config, "ACCOUNT_TOKENS", config.AccountTokens("foo:bar,login:token")
    )

    assert ut.get_token_for("foo")["oauth_access_token"] == "bar"
    assert ut.get_token_for("login")["oauth_access_token"] == "token"
    assert ut.get_token_for("nop") is None
