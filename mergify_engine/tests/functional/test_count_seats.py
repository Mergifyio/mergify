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

import yaml

from mergify_engine import config
from mergify_engine import count_seats
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine.tests.functional import base


class TestCountSeats(base.FunctionalTestBase):

    COLLABORATORS: count_seats.CollaboratorsT = {
        github_types.GitHubLogin("mergifyio-testing"): {
            github_types.GitHubRepositoryName("functional-testing-repo"): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
            github_types.GitHubRepositoryName("functional-testing-repo-private"): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
            github_types.GitHubRepositoryName("gh-action-tests"): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
            github_types.GitHubRepositoryName("git-pull-request"): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
            # TODO(sileht): Rework test to ignore developer related repositories
            github_types.GitHubRepositoryName(
                "functional-testing-repo-ErwanSimonetti"
            ): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
            github_types.GitHubRepositoryName("functional-testing-repo-sileht"): {
                "write": {
                    github_types.GitHubLogin("jd"),
                    github_types.GitHubLogin("mergify-test1"),
                    github_types.GitHubLogin("mergify-test3"),
                    github_types.GitHubLogin("mergify-test4"),
                    github_types.GitHubLogin("sileht"),
                },
                "active": set(),
            },
        },
    }

    async def _prepare_repo(self) -> count_seats.CollaboratorsT:
        await self.setup_repo()
        await self.create_pr()
        await self.run_engine()

        # NOTE(sileht): we add active users only on the repository used for
        # recording the fixture
        collaborators = copy.deepcopy(self.COLLABORATORS)
        active_users = collaborators[github_types.GitHubLogin("mergifyio-testing")][
            self.repository_ctxt.repo["name"]
        ]["active"]

        if self.client_admin.auth.owner_id is None:
            raise RuntimeError("client_admin owner_id is None")
        if self.client_fork.auth.owner_id is None:
            raise RuntimeError("client_fork owner_id is None")
        active_users.add(self.client_admin.auth.owner_id)
        active_users.add(self.client_fork.auth.owner_id)
        return collaborators

    async def test_get_collaborators(self) -> None:
        collaborators = await self._prepare_repo()
        print(await count_seats.get_collaborators())
        print(collaborators)
        assert await count_seats.get_collaborators() == collaborators

    async def test_count_seats(self) -> None:
        await self._prepare_repo()
        assert count_seats.count_seats(
            await count_seats.get_collaborators()
        ) == count_seats.SeatsCountResultT(5, 2)

    def test_run_count_seats_report(self) -> None:
        args = argparse.Namespace(json=True, daemon=False)
        with mock.patch("sys.stdout") as stdout:
            with mock.patch.object(config, "SUBSCRIPTION_TOKEN"):
                count_seats.report(args)
                s = "".join(call.args[0] for call in stdout.write.mock_calls)
                assert json.loads(s) == json.loads(json.dumps(self.COLLABORATORS))

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
        repo_id = self.RECORD_CONFIG["repository_id"]
        organization_id = self.RECORD_CONFIG["organization_id"]
        key = f"active-users~{organization_id}~{repo_id}"
        active_users = await self.redis_cache.zrangebyscore(
            key, min="-inf", max="+inf", withscores=True
        )
        now = time.time()
        assert len(active_users) == 2
        user_id_admin, timestamp_admin = active_users[0]
        user_id_fork, timestamp_fork = active_users[1]
        assert timestamp_admin <= now and timestamp_admin > now - 60
        assert user_id_admin == str(self.client_admin.auth.owner_id)
        assert timestamp_fork <= now and timestamp_fork > now - 60
        assert user_id_fork == str(self.client_fork.auth.owner_id)
