from unittest import mock

import pytest

from mergify_engine import subscription
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

    await sub.save_subscription_to_cache()
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
