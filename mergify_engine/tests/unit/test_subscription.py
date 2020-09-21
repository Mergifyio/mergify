import pytest

from mergify_engine import subscription


def test_init():
    subscription.Subscription(
        123, True, "friend", {}, frozenset({subscription.Features.PRIVATE_REPOSITORY})
    )


def test_dict():
    owner_id = 1234
    sub = subscription.Subscription(
        owner_id,
        True,
        "friend",
        {},
        frozenset({subscription.Features.PRIVATE_REPOSITORY}),
    )

    assert sub.from_dict(owner_id, sub.to_dict()) == sub


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
async def test_save_sub(features):
    owner_id = 1234
    sub = subscription.Subscription(owner_id, True, "friend", {}, frozenset(features))

    await sub.save_subscription_to_cache()
    rsub = await subscription.Subscription._retrieve_subscription_from_cache(owner_id)
    assert rsub == sub


@pytest.mark.asyncio
async def test_unknown_sub():
    sub = await subscription.Subscription._retrieve_subscription_from_cache(98732189)
    assert sub is None


def test_from_dict_unknown_features():
    assert subscription.Subscription.from_dict(
        123,
        {
            "subscription_active": True,
            "subscription_reason": "friend",
            "tokens": {},
            "features": ["unknown feature"],
        },
    ) == subscription.Subscription(
        123,
        True,
        "friend",
        {},
        frozenset(),
    )


def test_active_feature():
    sub = subscription.Subscription(
        123,
        True,
        "friend",
        {},
        frozenset(),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        123,
        False,
        "friend",
        {},
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is False
    sub = subscription.Subscription(
        123,
        True,
        "friend",
        {},
        frozenset([subscription.Features.PRIORITY_QUEUES]),
    )
    assert sub.has_feature(subscription.Features.PRIORITY_QUEUES) is True
