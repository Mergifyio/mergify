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
import yaml

from mergify_engine.tests.functional import base


class TestSubscriptionAction(base.FunctionalTestBase):
    async def test_subscription(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "subscription",
                    "conditions": [f"base={self.main_branch_name}"],
                    "actions": {
                        "subscription": {"subscribed": ["mergify-test1"]},
                        "label": {"add": ["foobar"]},
                    },
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "labeled"})
        resp = await self.client_admin.get(
            f"{self.repository_ctxt.base_url}/issues/{p['number']}/events",
        )
        events = resp.json()
        assert len(events) >= 2, [e["event"] for e in events]
        assert events[0]["event"] == "labeled"
        assert events[1]["event"] == "subscribed"
        assert events[1]["user"]["login"] == "mergify-test1"
