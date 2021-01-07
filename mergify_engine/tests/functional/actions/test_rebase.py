# -*- encoding: utf-8 -*-
#
#  Copyright © 2021 Mergify SAS
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

from mergify_engine import config
from mergify_engine.tests.functional import base

# où/cm simuler une nouvelle PR sur le repo
# où/cm similuer un repo à > 512Mo > config.NOSUB_MAX_REPO_SIZE_KB = 567 ?
# input: quel(s) test(s) faire pour vérification ? assert


class TestRebaseAction(base.FunctionalTestBase):
    def test_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rebase",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.run_engine()

        p.update()
        pass


class TestRebaseActionWithSub(base.FunctionalTestBase):
    SUBSCRIPTION_ACTIVE = True
    config.NOSUB_MAX_REPO_SIZE_KB = 567

    async def test_rebase_repo_over_512lim_ok(self):
        pass


class TestRebaseActionWithoutSub(base.FunctionalTestBase):
    config.NOSUB_MAX_REPO_SIZE_KB = 567

    async def test_rebase_repo_over_512lim_fail(self):
        pass
