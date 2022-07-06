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
import operator
import time
import typing
from unittest import mock

import yaml

from mergify_engine import config
from mergify_engine import count_seats
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine.tests.functional import base


class TestCountSeats(base.FunctionalTestBase):
    async def _prepare_repo(self) -> count_seats.Seats:
        await self.setup_repo()
        await self.create_pr(as_="admin")
        await self.create_pr(as_="fork")
        await self.run_engine()

        # NOTE(sileht): we add active users only on the repository used for
        # recording the fixture

        members = (
            await self.client_admin.get(
                url=f"{config.GITHUB_REST_API_URL}/orgs/mergifyio-testing/members"
            )
        ).json()
        write_users = {
            count_seats.SeatAccount(
                github_types.GitHubAccountIdType(member["id"]),
                github_types.GitHubLogin(member["login"]),
            )
            for member in members
        }
        organization = {}
        active_users = None
        key_repo = count_seats.SeatRepository(
            github_types.GitHubRepositoryIdType(self.repository_ctxt.repo["id"]),
            github_types.GitHubRepositoryName(self.repository_ctxt.repo["name"]),
        )
        active_users = {
            count_seats.ActiveUser(
                github_types.GitHubAccountIdType(config.TESTING_MERGIFY_TEST_1_ID),
                github_types.GitHubLogin("mergify-test1"),
            ),
            count_seats.ActiveUser(
                github_types.GitHubAccountIdType(config.TESTING_MERGIFY_TEST_2_ID),
                github_types.GitHubLogin("mergify-test2"),
            ),
        }
        organization[key_repo] = count_seats.CollaboratorsSetsT(
            {
                "write_users": write_users,
                "active_users": active_users,
            }
        )

        collaborators = {
            count_seats.SeatAccount(
                github_types.GitHubAccountIdType(config.TESTING_ORGANIZATION_ID),
                github_types.GitHubLogin(config.TESTING_ORGANIZATION_NAME),
            ): organization
        }

        return count_seats.Seats(collaborators)

    async def test_get_collaborators(self) -> None:
        expected_seats = await self._prepare_repo()
        assert (
            await count_seats.Seats.get(self.redis_links.active_users)
        ).seats == expected_seats.seats

    async def test_count_seats(self) -> None:
        await self._prepare_repo()
        members = (
            await self.client_admin.get(
                url=f"{config.GITHUB_REST_API_URL}/orgs/mergifyio-testing/members"
            )
        ).json()
        seats_count = (
            await count_seats.Seats.get(self.redis_links.active_users)
        ).count()
        assert seats_count.write_users >= len(members)
        assert seats_count.active_users == 2

    async def test_run_count_seats_report(self) -> None:
        await self.setup_repo()
        await self.create_pr(as_="admin")
        await self.create_pr(as_="fork")
        await self.run_engine()
        if github_types.GitHubAccountIdType(config.TESTING_MERGIFY_TEST_1_ID) is None:
            raise RuntimeError("client_admin owner_id is None")
        if github_types.GitHubAccountIdType(config.TESTING_MERGIFY_TEST_2_ID) is None:
            raise RuntimeError("client_fork owner_id is None")
        if github_types.GitHubLogin("mergify-test1") is None:
            raise RuntimeError("client_admin owner is None")
        if github_types.GitHubLogin("mergify-test2") is None:
            raise RuntimeError("client_fork owner is None")
        args = argparse.Namespace(json=True, daemon=False)
        with mock.patch("sys.stdout") as stdout:
            with mock.patch.object(config, "SUBSCRIPTION_TOKEN"):
                await count_seats.report(args)
                s = "".join(call.args[0] for call in stdout.write.mock_calls)
                json_reports = json.loads(s)
                assert list(json_reports.keys()) == ["organizations"]
                assert len(json_reports["organizations"]) == 1

                org = json_reports["organizations"][0]
                assert org["id"] == config.TESTING_ORGANIZATION_ID
                assert org["login"] == config.TESTING_ORGANIZATION_NAME

                assert len(org["repositories"]) == 1
                repo = org["repositories"][0]
                assert sorted(
                    repo["collaborators"]["active_users"],
                    key=operator.itemgetter("id"),
                ) == sorted(
                    [
                        {
                            "id": github_types.GitHubAccountIdType(
                                config.TESTING_MERGIFY_TEST_1_ID
                            ),
                            "login": github_types.GitHubLogin("mergify-test1"),
                        },
                        {
                            "id": github_types.GitHubAccountIdType(
                                config.TESTING_MERGIFY_TEST_2_ID
                            ),
                            "login": github_types.GitHubLogin("mergify-test2"),
                        },
                    ],
                    key=operator.itemgetter("id"),
                )
                assert len(repo["collaborators"]["active_users"]) == 2

    async def test_stored_user_in_redis(self) -> None:
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
        await self.create_pr(as_="admin")
        await self.create_pr(as_="fork")
        await self.run_engine()
        repository_id = self.RECORD_CONFIG["repository_id"]
        organization_id = self.RECORD_CONFIG["organization_id"]
        repository_name = self.RECORD_CONFIG["repository_name"]
        organization_name = self.RECORD_CONFIG["organization_name"]
        key = f"active-users~{organization_id}~{organization_name}~{repository_id}~{repository_name}"
        active_users: typing.List[
            typing.Tuple[bytes, float]
        ] = await self.redis_links.active_users.zrangebyscore(
            key, min="-inf", max="+inf", withscores=True
        )
        now = time.time()
        assert len(active_users) == 2
        user_admin, timestamp_admin = active_users[0]
        user_fork, timestamp_fork = active_users[1]
        assert timestamp_admin <= now and timestamp_admin > now - 60
        assert (
            user_admin == f"{config.TESTING_MERGIFY_TEST_1_ID}~mergify-test1".encode()
        )
        assert timestamp_fork <= now and timestamp_fork > now - 60
        assert user_fork == f"{config.TESTING_MERGIFY_TEST_2_ID}~mergify-test2".encode()
