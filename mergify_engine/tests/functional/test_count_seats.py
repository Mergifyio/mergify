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
from unittest import mock

from mergify_engine import config
from mergify_engine import count_seats
from mergify_engine import json
from mergify_engine.tests.functional import base


class TestCountSeats(base.FunctionalTestBase):

    COLLABORATORS = {
        "mergifyio-testing": {
            "functional-testing-repo": {
                "jd",
                "mergify-test1",
                "mergify-test3",
                "mergify-test4",
                "sileht",
            },
            "functional-testing-repo-private": {
                "jd",
                "mergify-test1",
                "mergify-test3",
                "mergify-test4",
                "sileht",
            },
            "gh-action-tests": {
                "jd",
                "mergify-test1",
                "mergify-test3",
                "mergify-test4",
                "sileht",
            },
            "git-pull-request": {
                "jd",
                "mergify-test1",
                "mergify-test3",
                "mergify-test4",
                "sileht",
            },
        },
    }

    async def test_get_collaborators(self) -> None:
        assert await count_seats.get_collaborators() == self.COLLABORATORS

    async def test_count_seats(self) -> None:
        assert count_seats.count_seats(await count_seats.get_collaborators()) == 5

    def test_run_count_seats_report(self) -> None:
        args = argparse.Namespace(json=True, daemon=False)
        with mock.patch("sys.stdout") as stdout:
            with mock.patch.object(config, "SUBSCRIPTION_TOKEN"):
                count_seats.report(args)
                s = "".join(call.args[0] for call in stdout.write.mock_calls)
                assert json.loads(s) == self.COLLABORATORS
