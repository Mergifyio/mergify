from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import pytest

from mergify_engine import config
from mergify_engine import subscription
from mergify_engine import subscription_key
from mergify_engine.clients import http


@pytest.mark.asyncio
async def test_init(redis_cache):
    subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        frozenset({subscription.Features.PRIVATE_REPOSITORY}),
    )


@pytest.mark.asyncio
async def test_dict(redis_cache):
    owner_id = 1234
    sub = subscription.Subscription(
        redis_cache,
        owner_id,
        True,
        "friend",
        frozenset({subscription.Features.PRIVATE_REPOSITORY}),
    )

    assert sub.from_dict(redis_cache, owner_id, sub.to_dict(), -2) == sub


@pytest.mark.parametrize(
    "features",
    (
        {},
        {subscription.Features.PRIVATE_REPOSITORY},
        {
            subscription.Features.PRIVATE_REPOSITORY,
            subscription.Features.PRIORITY_QUEUES,
        },
    ),
)
@pytest.mark.asyncio
async def test_save_sub(features, redis_cache):
    owner_id = 1234
    sub = subscription.Subscription(
        redis_cache,
        owner_id,
        True,
        "friend",
        frozenset(features),
    )

    await sub._save_subscription_to_cache()
    rsub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, owner_id
    )
    assert rsub == sub


@pytest.mark.asyncio
@mock.patch.object(subscription.Subscription, "_retrieve_subscription_from_db")
async def test_subscription_db_unavailable(
    retrieve_subscription_from_db_mock, redis_cache
):
    owner_id = 1234
    sub = subscription.Subscription(redis_cache, owner_id, True, "friend", frozenset())
    retrieve_subscription_from_db_mock.return_value = sub

    # no cache, no db -> reraise
    retrieve_subscription_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    with pytest.raises(http.HTTPServiceUnavailable):
        await subscription.Subscription.get_subscription(redis_cache, owner_id)
        retrieve_subscription_from_db_mock.assert_called_once()

    # no cache, but db -> got db sub
    retrieve_subscription_from_db_mock.reset_mock()
    retrieve_subscription_from_db_mock.side_effect = None
    rsub = await subscription.Subscription.get_subscription(redis_cache, owner_id)
    assert sub == rsub
    retrieve_subscription_from_db_mock.assert_called_once()

    # cache not expired and not db -> got cached  sub
    retrieve_subscription_from_db_mock.reset_mock()
    rsub = await subscription.Subscription.get_subscription(redis_cache, owner_id)
    sub.ttl = 259200
    assert rsub == sub
    retrieve_subscription_from_db_mock.assert_not_called()

    # cache expired and not db -> got cached  sub
    retrieve_subscription_from_db_mock.reset_mock()
    retrieve_subscription_from_db_mock.side_effect = http.HTTPServiceUnavailable(
        "boom!", response=mock.Mock(), request=mock.Mock()
    )
    await redis_cache.expire(f"subscription-cache-owner-{owner_id}", 7200)
    rsub = await subscription.Subscription.get_subscription(redis_cache, owner_id)
    sub.ttl = 7200
    assert rsub == sub
    retrieve_subscription_from_db_mock.assert_called_once()

    # cache expired and unexpected db issue -> reraise
    retrieve_subscription_from_db_mock.reset_mock()
    retrieve_subscription_from_db_mock.side_effect = Exception("WTF")
    await redis_cache.expire(f"subscription-cache-owner-{owner_id}", 7200)
    with pytest.raises(Exception):
        await subscription.Subscription.get_subscription(redis_cache, owner_id)
    retrieve_subscription_from_db_mock.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_sub(redis_cache):
    sub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, 98732189
    )
    assert sub is None


@pytest.mark.asyncio
async def test_from_dict_unknown_features(redis_cache):
    assert (
        subscription.Subscription.from_dict(
            redis_cache,
            123,
            {
                "subscription_active": True,
                "subscription_reason": "friend",
                "features": ["unknown feature"],
            },
        )
        == subscription.Subscription(redis_cache, 123, True, "friend", frozenset(), -2)
    )


@pytest.mark.asyncio
async def test_active_feature(redis_cache):
    sub = subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        frozenset(),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        redis_cache,
        123,
        False,
        "friend",
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is True

    sub = subscription.Subscription.from_dict(
        redis_cache,
        123,
        {
            "subscription_active": True,
            "subscription_reason": "friend",
            "features": ["private_repository", "large_repository"],
        },
    )
    assert sub.has_feature(subscription.Features.PRIVATE_REPOSITORY) is True
    assert sub.has_feature(subscription.Features.LARGE_REPOSITORY) is True
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False


@pytest.mark.asyncio
async def test_subscription_key_generators(capsys, monkeypatch, redis_cache):
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_key_str = (
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .strip()
        .split(b"\n")[1]
        .decode()
    )
    monkeypatch.setattr(config, "SUBSCRIPTION_PRIVATE_KEY", private_key_str)
    monkeypatch.setattr(
        subscription_key, "SUBSCRIPTION_PUBLIC_KEY", private_key.public_key()
    )

    monkeypatch.setattr("sys.argv", ["mergify-subscitption-generator", "12345"])
    ret = subscription_key.generate()
    assert ret == 0
    key = capsys.readouterr().out.strip()

    data = subscription_key.DecryptedSubscriptionKey(key)
    assert data == {
        12345: {
            "subscription_active": True,
            "subscription_reason": "Subscription for 12345 is active",
            "tokens": {},
            "features": [
                "private_repository",
                "large_repository",
                "priority_queues",
                "custom_checks",
                "random_request_reviews",
                "merge_bot_account",
                "bot_account",
                "queue_action",
            ],
        }
    }

    monkeypatch.setattr(
        config, "BOT_ACCOUNTS", config.BotAccounts("foo:bar,login:token")
    )
    monkeypatch.setattr(config, "SUBSCRIPTION_KEY", data)

    sub = await subscription.Subscription.get_subscription(redis_cache, 12345)
    assert sub.active
    assert sub.get_token_for("foo") == "bar"
    assert sub.get_token_for("login") == "token"
    assert sub.get_token_for("nop") is None

    sub = await subscription.Subscription.get_subscription(redis_cache, 54321)
    assert not sub.active
