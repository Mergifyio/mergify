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
import copy
import time
from unittest import mock

import pytest
import yaml

from mergify_engine import config
from mergify_engine import count_seats
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine.tests.functional import base


REPOSITORIES = (
    (258840104, "functional-testing-repo"),
    (399462964, "functional-testing-repo-ErwanSimonetti"),
    (265231765, "functional-testing-repo-private"),
    (399426199, "functional-testing-repo-sileht"),
    (269274944, "gh-action-tests"),
    (210296945, "git-pull-request"),
)


@pytest.mark.skip(
    "Thoses tests aren't reliable, the testing organization could change repo-wise, user-wise etc."
)
class TestCountSeats(base.FunctionalTestBase):
    ORGANIZATION = count_seats.SeatAccount(
        github_types.GitHubAccountIdType(40527191),
        github_types.GitHubLogin("mergifyio-testing"),
    )

    COLLABORATORS: count_seats.CollaboratorsT = {
        ORGANIZATION: {
            count_seats.SeatRepository(
                github_types.GitHubRepositoryIdType(_id),
                github_types.GitHubRepositoryName(_name),
            ): {
                "write_users": {
                    count_seats.SeatAccount(
                        github_types.GitHubAccountIdType(2644),
                        github_types.GitHubLogin("jd"),
                    ),
                    count_seats.SeatAccount(
                        github_types.GitHubAccountIdType(38494943),
                        github_types.GitHubLogin("mergify-test1"),
                    ),
                    count_seats.SeatAccount(
                        github_types.GitHubAccountIdType(58822980),
                        github_types.GitHubLogin("mergify-test3"),
                    ),
                    count_seats.SeatAccount(
                        github_types.GitHubAccountIdType(74646794),
                        github_types.GitHubLogin("mergify-test4"),
                    ),
                    count_seats.SeatAccount(
                        github_types.GitHubAccountIdType(200878),
                        github_types.GitHubLogin("sileht"),
                    ),
                },
                "active_users": None,
            }
            for _id, _name in REPOSITORIES
        }
    }

    async def _prepare_repo(self) -> count_seats.Seats:
        await self.setup_repo()
        await self.create_pr()
        await self.run_engine()

        # NOTE(sileht): we add active users only on the repository used for
        # recording the fixture
        collaborators = copy.deepcopy(self.COLLABORATORS)
        repository = collaborators[self.ORGANIZATION][
            count_seats.SeatRepository(
                self.repository_ctxt.repo["id"], self.repository_ctxt.repo["name"]
            )
        ]

        if self.client_admin.auth.owner_id is None:
            raise RuntimeError("client_admin owner_id is None")
        if self.client_fork.auth.owner_id is None:
            raise RuntimeError("client_fork owner_id is None")
        if self.client_admin.auth.owner is None:
            raise RuntimeError("client_admin owner is None")
        if self.client_fork.auth.owner is None:
            raise RuntimeError("client_fork owner is None")
        repository["active_users"] = {
            count_seats.ActiveUser(
                self.client_admin.auth.owner_id, self.client_admin.auth.owner
            ),
            count_seats.ActiveUser(
                self.client_fork.auth.owner_id, self.client_fork.auth.owner
            ),
        }
        return count_seats.Seats(collaborators)

    async def test_get_collaborators(self) -> None:
        expected_seats = await self._prepare_repo()
        assert (
            await count_seats.Seats.get(self.redis_cache)
        ).seats == expected_seats.seats

    async def test_count_seats(self) -> None:
        await self._prepare_repo()
        assert (
            await count_seats.Seats.get(self.redis_cache)
        ).count() == count_seats.SeatsCountResultT(5, 2)

    @pytest.mark.skip(
        reason="The tests shouldn't have hardcoded repos and users in case of change in the testing organization."
    )
    async def test_run_count_seats_report(self) -> None:
        args = argparse.Namespace(json=True, daemon=False)
        with mock.patch("sys.stdout") as stdout:
            with mock.patch.object(config, "SUBSCRIPTION_TOKEN"):
                await count_seats.report(args)
                s = "".join(call.args[0] for call in stdout.write.mock_calls)
                expected_seats = count_seats.Seats(self.COLLABORATORS)
                assert json.loads(s) == expected_seats.jsonify()

    async def test_stored_user_in_redis(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "no-draft",
                    "conditions": ["created-at<9999 days ago"],
                    "actions": {"comment": {"message": "it's time"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        await self.create_pr()
        await self.run_engine()
        repository_id = self.RECORD_CONFIG["repository_id"]
        organization_id = self.RECORD_CONFIG["organization_id"]
        repository_name = self.RECORD_CONFIG["repository_name"]
        organization_name = self.RECORD_CONFIG["organization_name"]
        key = f"active-users~{organization_id}~{organization_name}~{repository_id}~{repository_name}"
        active_users = await self.redis_cache.zrangebyscore(
            key, min="-inf", max="+inf", withscores=True
        )
        now = time.time()
        assert len(active_users) == 2
        user_admin, timestamp_admin = active_users[0]
        user_fork, timestamp_fork = active_users[1]
        assert timestamp_admin <= now and timestamp_admin > now - 60
        assert (
            user_admin
            == f"{self.client_admin.auth.owner_id}~{self.client_admin.auth.owner}"
        )
        assert timestamp_fork <= now and timestamp_fork > now - 60
        assert (
            user_fork
            == f"{self.client_fork.auth.owner_id}~{self.client_fork.auth.owner}"
        )
