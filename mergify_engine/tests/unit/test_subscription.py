from unittest import mock

import pytest
from pytest_httpserver import httpserver

from mergify_engine import exceptions
from mergify_engine import redis_utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription


async def test_init(redis_cache):
    subscription.Subscription(
        redis_cache,
        123,
        "friend",
        frozenset({subscription.Features.PRIVATE_REPOSITORY}),
    )


async def test_dict(redis_cache):
    owner_id = 1234
    sub = subscription.Subscription(
        redis_cache,
        owner_id,
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
            subscription.Features.PUBLIC_REPOSITORY,
            subscription.Features.PRIORITY_QUEUES,
        },
    ),
)
async def test_save_sub(features, redis_cache):
    owner_id = 1234
    sub = subscription.Subscription(
        redis_cache,
        owner_id,
        "friend",
        frozenset(features),
    )

    await sub._save_subscription_to_cache()
    rsub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, owner_id
    )
    assert rsub == sub


@mock.patch.object(subscription.Subscription, "_retrieve_subscription_from_db")
async def test_subscription_db_unavailable(
    retrieve_subscription_from_db_mock, redis_cache
):
    owner_id = 1234
    sub = subscription.Subscription(
        redis_cache,
        owner_id,
        "friend",
        frozenset([subscription.Features.PUBLIC_REPOSITORY]),
    )
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


async def test_unknown_sub(redis_cache):
    sub = await subscription.Subscription._retrieve_subscription_from_cache(
        redis_cache, 98732189
    )
    assert sub is None


async def test_from_dict_unknown_features(redis_cache):
    assert subscription.Subscription.from_dict(
        redis_cache,
        123,
        {
            "subscription_reason": "friend",
            "features": ["unknown feature"],
        },
    ) == subscription.Subscription(
        redis_cache,
        123,
        "friend",
        frozenset(),
        -2,
    )


async def test_active_feature(redis_cache):
    sub = subscription.Subscription(
        redis_cache,
        123,
        "friend",
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )

    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is True
    sub = subscription.Subscription(
        redis_cache,
        123,
        "friend",
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is True

    sub = subscription.Subscription.from_dict(
        redis_cache,
        123,
        {
            "subscription_reason": "friend",
            "features": ["private_repository"],
        },
    )
    assert sub.has_feature(subscription.Features.PRIVATE_REPOSITORY) is True
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False


async def test_subscription_on_premise_valid(
    redis_cache: redis_utils.RedisCache,
    httpserver: httpserver.HTTPServer,
) -> None:

    httpserver.expect_request("/on-premise/subscription/1234").respond_with_json(
        {
            "subscription_reason": "azertyuio",
            "features": [
                "private_repository",
                "public_repository",
                "priority_queues",
                "custom_checks",
                "random_request_reviews",
                "merge_bot_account",
                "queue_action",
                "show_sponsor",
            ],
        }
    )

    with mock.patch(
        "mergify_engine.config.SUBSCRIPTION_BASE_URL",
        httpserver.url_for("/")[:-1],
    ):
        await subscription.SubscriptionDashboardOnPremise.get_subscription(
            redis_cache, 1234
        )

    assert len(httpserver.log) == 1
    httpserver.check_assertions()  # type: ignore [no-untyped-call]


async def test_subscription_on_premise_wrong_token(
    redis_cache: redis_utils.RedisCache,
    httpserver: httpserver.HTTPServer,
) -> None:

    httpserver.expect_request("/on-premise/subscription/1234").respond_with_json(
        {"message": "error"}, status=401
    )

    with mock.patch(
        "mergify_engine.config.SUBSCRIPTION_BASE_URL",
        httpserver.url_for("/")[:-1],
    ):
        with pytest.raises(exceptions.MergifyNotInstalled):
            await subscription.SubscriptionDashboardOnPremise.get_subscription(
                redis_cache, 1234
            )

    assert len(httpserver.log) == 1
    httpserver.check_assertions()  # type: ignore [no-untyped-call]


async def test_subscription_on_premise_invalid_sub(
    redis_cache: redis_utils.RedisCache,
    httpserver: httpserver.HTTPServer,
) -> None:

    httpserver.expect_request("/on-premise/subscription/1234").respond_with_json(
        {"message": "error"}, status=403
    )

    with mock.patch(
        "mergify_engine.config.SUBSCRIPTION_BASE_URL",
        httpserver.url_for("/")[:-1],
    ):
        with pytest.raises(exceptions.MergifyNotInstalled):
            await subscription.SubscriptionDashboardOnPremise.get_subscription(
                redis_cache, 1234
            )

    assert len(httpserver.log) == 1
    httpserver.check_assertions()  # type: ignore [no-untyped-call]
