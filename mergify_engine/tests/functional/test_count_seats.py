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


from mergify_engine import count_seats
from mergify_engine.tests.functional import base


class TestCountSeats(base.FunctionalTestBase):
    async def test_get_collaborators(self):
        assert await count_seats.get_collaborators() == {
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

    async def test_count_seats(self):
        assert count_seats.count_seats(await count_seats.get_collaborators()) == 5
