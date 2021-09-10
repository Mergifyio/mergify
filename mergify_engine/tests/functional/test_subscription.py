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

from mergify_engine import config
from mergify_engine import context
from mergify_engine import subscription
from mergify_engine.tests.functional import base


class TestSubscription(base.FunctionalTestBase):
    async def test_subscription(self):
        self.subscription = subscription.Subscription(
            self.redis_cache,
            config.TESTING_ORGANIZATION_ID,
            self.SUBSCRIPTION_ACTIVE,
            "Abuse",
            frozenset(
                getattr(subscription.Features, f)
                for f in subscription.Features.__members__
            )
            if self.SUBSCRIPTION_ACTIVE
            else frozenset([]),
        )

        await self.setup_repo()
        p, _ = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks_abuse = await ctxt.pull_engine_check_runs
        assert len(checks_abuse) == 1
        assert checks_abuse[0]["name"] == "Summary"
        assert "Abuse" == checks_abuse[0]["output"]["summary"]
        assert "Mergify is disabled" == checks_abuse[0]["output"]["title"]
