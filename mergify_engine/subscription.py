# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
import dataclasses
import enum
import json
import typing

import daiquiri

from mergify_engine import config
from mergify_engine import crypto
from mergify_engine import utils
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)


@enum.unique
class Features(enum.Enum):
    PRIVATE_REPOSITORY = "private_repository"
    LARGE_REPOSITORY = "large_repository"
    PRIORITY_QUEUES = "priority_queues"
    CUSTOM_CHECKS = "custom_checks"
    RANDOM_REQUEST_REVIEWS = "random_request_reviews"
    MERGE_BOT_ACCOUNT = "merge_bot_account"


@dataclasses.dataclass
class Subscription:
    owner_id: int
    active: bool
    reason: str
    tokens: typing.Dict[str, str]
    features: typing.FrozenSet[enum.Enum]

    @staticmethod
    def _to_features(feature_list):
        features = []
        for f in feature_list:
            try:
                feature = Features(f)
            except ValueError:
                LOG.error("Unknown subscription feature %s", f)
            else:
                features.append(feature)
        return frozenset(features)

    def has_feature(self, feature: Features):
        """Return if the feature for a plan is available."""
        return self.active and feature in self.features

    @staticmethod
    def missing_feature_reason(owner):
        return f"⚠ The [subscription](https://dashboard.mergify.io/github/{owner}/subscription) needs to be updated to enable this feature."

    @classmethod
    def from_dict(cls, owner_id, sub):
        return cls(
            owner_id,
            sub["subscription_active"],
            sub["subscription_reason"],
            sub["tokens"],
            cls._to_features(sub.get("features", [])),
        )

    def get_token_for(self, wanted_login: str) -> typing.Optional[str]:
        wanted_login = wanted_login.lower()
        for login, token in self.tokens.items():
            if login.lower() == wanted_login:
                return token
        return None

    def to_dict(self):
        return {
            "subscription_active": self.active,
            "subscription_reason": self.reason,
            "tokens": self.tokens,
            "features": list(f.value for f in self.features),
        }

    @classmethod
    async def get_subscription(cls, owner_id):
        """Get a subscription."""
        sub = await cls._retrieve_subscription_from_cache(owner_id)
        if sub is None:
            sub = await cls._retrieve_subscription_from_db(owner_id)
            await sub.save_subscription_to_cache()
        return sub

    async def save_subscription_to_cache(self):
        """Save a subscription to the cache."""
        r = await utils.get_aredis_for_cache()
        await r.setex(
            "subscription-cache-owner-%s" % self.owner_id,
            3600,
            crypto.encrypt(json.dumps(self.to_dict()).encode()),
        )

    @classmethod
    async def _retrieve_subscription_from_db(cls, owner_id):
        LOG.info("Subscription not cached, retrieving it...", gh_owner=owner_id)
        async with http.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{config.SUBSCRIPTION_BASE_URL}/engine/github-account/{owner_id}",
                    auth=(config.OAUTH_CLIENT_ID, config.OAUTH_CLIENT_SECRET),
                )
            except http.HTTPNotFound as e:
                return cls(owner_id, False, e.message, {}, frozenset())
            else:
                sub = resp.json()
                sub["tokens"] = dict(
                    (login, token["access_token"])
                    for login, token in sub["tokens"].items()
                )
                return cls.from_dict(owner_id, sub)

    @classmethod
    async def _retrieve_subscription_from_cache(cls, owner_id):
        r = await utils.get_aredis_for_cache()
        encrypted_sub = await r.get("subscription-cache-owner-%s" % owner_id)
        if encrypted_sub:
            return cls.from_dict(
                owner_id, json.loads(crypto.decrypt(encrypted_sub).decode())
            )
