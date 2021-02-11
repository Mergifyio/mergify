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


import argparse
import base64
import typing

import cryptography.exceptions
from cryptography.hazmat.primitives import serialization
import msgpack
import pydantic

from mergify_engine import types


SUBSCRIPTION_PUBLIC_KEY = serialization.load_pem_public_key(
    b"""
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAGT7hNLq04eEx7oix6pVcAJhPMS33ChVF6wIS/P7fr5w=
-----END PUBLIC KEY-----
""",
    None,
)


# TODO(sileht): when https://github.com/samuelcolvin/pydantic/pull/2216 is released
# we can directly use typing.TypedDict into pydantic Model
# in the meantime we duplicate the type here
class SubscriptionDictModel(pydantic.BaseModel):
    subscription_active: bool
    subscription_reason: str
    tokens: typing.Dict[str, str]
    features: typing.List[
        typing.Literal[
            "private_repository",
            "large_repository",
            "priority_queues",
            "custom_checks",
            "random_request_reviews",
            "merge_bot_account",
            "bot_account",
            "queue_action",
        ]
    ]


class SubscriptionKey(pydantic.BaseModel):
    data: bytes
    signature: bytes


class SubscriptionKeyData(pydantic.BaseModel):
    subscriptions: typing.Dict[int, SubscriptionDictModel]


def DecryptedSubscriptionKey(key: str) -> typing.Dict[int, types.SubscriptionDict]:
    try:
        data = msgpack.unpackb(base64.b64decode(key.encode()))
    except Exception:
        raise ValueError("incorrect subscription key format")

    try:
        subscription_key = SubscriptionKey(**data)
    except pydantic.ValidationError:
        raise ValueError("incorrect subscription key data")

    try:
        SUBSCRIPTION_PUBLIC_KEY.verify(
            subscription_key.signature, subscription_key.data
        )  # type: ignore
    except cryptography.exceptions.InvalidSignature:
        raise ValueError("incorrect subscription key signature")

    try:
        subscriptions_data = msgpack.unpackb(
            subscription_key.data, strict_map_key=False
        )
    except Exception:
        raise ValueError("incorrect subscription key listing format")

    try:
        return {
            owner_id: typing.cast(types.SubscriptionDict, subscription.dict())
            for owner_id, subscription in SubscriptionKeyData(
                **subscriptions_data
            ).subscriptions.items()
        }
    except pydantic.ValidationError:
        raise ValueError("incorrect subscription key listing data")


"""
Key pair generated with:

>>> from cryptography.hazmat.primitives.asymmetric import ed25519
>>> private_key = ed25519.Ed25519PrivateKey.generate()
>>> print(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
>>> print(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
"""


def generate() -> int:
    # NOTE(sileht): Avoid circular import issue
    from mergify_engine import config  # noqa
    from mergify_engine.subscription import Features  # noqa

    if config.SUBSCRIPTION_PRIVATE_KEY is None:
        print("Can generate private key with SUBSCRIPTION_PRIVATE_KEY not set")
        return 1

    private_key = serialization.load_pem_private_key(
        f"""
-----BEGIN PRIVATE KEY-----
{config.SUBSCRIPTION_PRIVATE_KEY}
-----END PRIVATE KEY-----
""".encode(),
        None,
    )
    expected_public_key = SUBSCRIPTION_PUBLIC_KEY.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    current_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    if current_public_key != expected_public_key:
        print("Unexpected private key")
        return 1

    parser = argparse.ArgumentParser(description="Generate subscription key")
    parser.add_argument(
        "owners",
        metavar="N",
        type=int,
        nargs="+",
        help="List of owner id for the subscription",
    )
    args = parser.parse_args()

    subscriptions = {}
    for owner in args.owners:
        subscriptions[owner] = SubscriptionDictModel(
            subscription_active=True,
            subscription_reason=f"Subscription for {owner} is active",
            tokens=[],
            features=[getattr(Features, f).value for f in Features.__members__],
        )
    data = msgpack.packb(
        SubscriptionKeyData(subscriptions=subscriptions).dict(), use_bin_type=True
    )
    signature = private_key.sign(data)  # type: ignore
    subscription_key = SubscriptionKey(data=data, signature=signature)
    print(
        base64.b64encode(
            msgpack.packb(subscription_key.dict(), use_bin_type=True)
        ).decode()
    )
    return 0
