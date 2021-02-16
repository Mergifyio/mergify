import pytest

from mergify_engine import config
from mergify_engine import subscription


@pytest.mark.asyncio
async def test_init(redis_cache):
    subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        {},
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
        {},
        frozenset({subscription.Features.PRIVATE_REPOSITORY}),
    )

    assert sub.from_dict(redis_cache, owner_id, sub.to_dict()) == sub


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
        redis_cache, owner_id, True, "friend", {}, frozenset(features)
    )

    await sub.save_subscription_to_cache()
    rsub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, owner_id
    )
    assert rsub == sub


@pytest.mark.asyncio
async def test_unknown_sub(redis_cache):
    sub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, 98732189
    )
    assert sub is None


@pytest.mark.asyncio
async def test_from_dict_unknown_features(redis_cache):
    assert subscription.Subscription.from_dict(
        redis_cache,
        123,
        {
            "subscription_active": True,
            "subscription_reason": "friend",
            "tokens": {},
            "features": ["unknown feature"],
        },
    ) == subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        {},
        frozenset(),
    )


@pytest.mark.asyncio
async def test_active_feature(redis_cache):
    sub = subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        {},
        frozenset(),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        redis_cache,
        123,
        False,
        "friend",
        {},
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        {},
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is True


@pytest.mark.asyncio
async def test_subscription_tokens_via_env(monkeypatch, redis_cache):
    sub = subscription.Subscription(
        redis_cache,
        123,
        True,
        "friend",
        {},
        frozenset(),
    )

    assert sub.get_token_for("foo") is None
    assert sub.get_token_for("login") is None
    assert sub.get_token_for("nop") is None

    monkeypatch.setattr(
        config, "ACCOUNT_TOKENS", config.AccountTokens("foo:bar,login:token")
    )

    assert sub.get_token_for("foo") == "bar"
    assert sub.get_token_for("login") == "token"
    assert sub.get_token_for("nop") is None
