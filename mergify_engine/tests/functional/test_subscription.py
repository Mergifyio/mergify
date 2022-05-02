# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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

from unittest import mock

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.dashboard import subscription
from mergify_engine.tests.functional import base


class TestSubscription(base.FunctionalTestBase):
    async def test_subscription(self) -> None:
        async def fake_subscription(
            redis_cache: redis_utils.RedisCache,
            owner_id: github_types.GitHubAccountIdType,
        ) -> subscription.Subscription:
            return subscription.Subscription(
                self.redis_links.cache,
                config.TESTING_ORGANIZATION_ID,
                "Abuse",
                frozenset(
                    getattr(subscription.Features, f)
                    for f in subscription.Features.__members__
                )
                if self.SUBSCRIPTION_ACTIVE
                else frozenset([]),
            )

        patcher = mock.patch(
            "mergify_engine.dashboard.subscription.Subscription._retrieve_subscription_from_db",
            side_effect=fake_subscription,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        await self.setup_repo()
        p = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
        assert summary is not None
        assert "Abuse" == summary["output"]["summary"]
        assert "Mergify is disabled" == summary["output"]["title"]
