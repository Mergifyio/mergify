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

from mergify_engine import context
from mergify_engine.tests.functional import base


class TestCommandSquash(base.FunctionalTestBase):
    async def test_squash_several_commits_ok(self):
        await self.setup_repo()
        repo_name = "fork"
        branch_name = self.get_full_branch_name(f"{repo_name}/pr_squash_test")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{self.master_branch_name}",
            "-b",
            branch_name,
        )

        for i in range(0, 3):
            open(self.git.tmp + f"/file{i}", "wb").close()
            await self.git("add", f"file{i}")
            await self.git("commit", "--no-edit", "-m", f"feat(): add file{i+1}")

        await self.git("push", "--quiet", repo_name, branch_name)

        client = self.client_fork
        owner_name = self.client_fork.auth.owner

        # create a PR with several commits to squash
        pr = (
            await client.post(
                f"{self.url_main}/pulls",
                json={
                    "base": self.master_branch_name,
                    "head": f"{owner_name}:{branch_name}",
                    "title": "squash the PR",
                    "body": "This is a squash_test",
                    "draft": False,
                },
            )
        ).json()

        await self.wait_for("pull_request", {"action": "opened"})

        commits = await self.get_commits(pr["number"])
        assert len(commits) > 1
        await self.run_engine()

        # do the squash
        await self.create_comment(pr["number"], "@mergifyio squash")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        # get the PR
        pr = await client.item(f"{self.url_main}/pulls/{pr['number']}")
        assert pr["commits"] == 1

    async def test_squash_several_commits_with_one_merge_commit_and_custom_message(
        self,
    ):
        await self.setup_repo()
        repo_name = "fork"
        branch_name1 = self.get_full_branch_name(f"{repo_name}/pr_squash_test_b1")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{self.master_branch_name}",
            "-b",
            branch_name1,
        )

        open(self.git.tmp + "/file0", "wb").close()
        await self.git("add", "file0")
        await self.git("commit", "--no-edit", "-m", "feat(): add file0")

        await self.git("push", "--quiet", repo_name, branch_name1)

        branch_name2 = self.get_full_branch_name(f"{repo_name}/pr_squash_test_b2")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{branch_name1}",
            "-b",
            branch_name2,
        )

        for i in range(1, 4):
            open(self.git.tmp + f"/file{i}", "wb").close()
            await self.git("add", f"file{i}")
            await self.git("commit", "--no-edit", "-m", f"feat(): add file{i}")

        await self.git("push", "--quiet", repo_name, branch_name2)

        client = self.client_fork
        owner_name = self.client_fork.auth.owner

        # create a merge between this 2 branches
        await client.post(
            f"{self.url_fork}/merges",
            json={
                "owner": owner_name,
                "repo": self.REPO_NAME,
                "base": branch_name1,
                "head": branch_name2,
            },
        )

        # create a new PR between main & fork
        pr = (
            await client.post(
                f"{self.url_main}/pulls",
                json={
                    "base": self.master_branch_name,
                    "head": f"{owner_name}:{branch_name1}",
                    "title": "squash the PR",
                    "body": """This is a squash_test

# Commit message
Awesome title

Awesome body
""",
                    "draft": False,
                },
            )
        ).json()

        await self.wait_for("pull_request", {"action": "opened"})

        commits = await self.get_commits(pr["number"])
        assert len(commits) > 1
        await self.run_engine()

        # do the squash
        await self.create_comment(pr["number"], "@mergifyio squash")
        await self.run_engine()

        # wait after the update & get the PR
        await self.wait_for("pull_request", {"action": "synchronize"})

        pr = await client.item(f"{self.url_main}/pulls/{pr['number']}")
        assert pr["commits"] == 1

        ctxt = await context.Context.create(self.repository_ctxt, pr, [])
        assert (await ctxt.commits)[0]["commit"][
            "message"
        ] == "Awesome title\n\nAwesome body"

    async def test_squash_several_commits_with_two_merge_commits(self):
        await self.setup_repo()
        repo_name = "fork"

        branch_name1 = self.get_full_branch_name(f"{repo_name}/pr_squash_test_b1")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{self.master_branch_name}",
            "-b",
            branch_name1,
        )

        open(self.git.tmp + "/file0", "wb").close()
        await self.git("add", "file0")
        await self.git("commit", "--no-edit", "-m", "feat(): add file0")

        await self.git("push", "--quiet", repo_name, branch_name1)

        branch_name2 = self.get_full_branch_name(f"{repo_name}/pr_squash_test_b2")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{branch_name1}",
            "-b",
            branch_name2,
        )

        for i in range(1, 4):
            open(self.git.tmp + f"/file{i}", "wb").close()
            await self.git("add", f"file{i}")
            await self.git("commit", "--no-edit", "-m", f"feat(): add file{i}")

        await self.git("push", "--quiet", repo_name, branch_name2)

        client = self.client_fork
        owner_name = self.client_fork.auth.owner

        # create a merge between this 2 branches
        await client.post(
            f"{self.url_fork}/merges",
            json={
                "owner": owner_name,
                "repo": self.REPO_NAME,
                "base": branch_name1,
                "head": branch_name2,
            },
        )

        branch_name3 = self.get_full_branch_name(f"{repo_name}/pr_squash_test_b3")

        await self.git(
            "checkout",
            "--quiet",
            f"{repo_name}/{self.master_branch_name}",
            "-b",
            branch_name3,
        )

        open(self.git.tmp + "/an_other_file", "wb").close()
        await self.git("add", "an_other_file")
        await self.git("commit", "--no-edit", "-m", "feat(): add an other file")

        await self.git("push", "--quiet", repo_name, branch_name3)

        # create a PR between this branche n3 & n1
        await client.post(
            f"{self.url_fork}/merges",
            json={
                "owner": owner_name,
                "repo": self.REPO_NAME,
                "base": branch_name1,
                "head": branch_name3,
            },
        )

        # create a new PR between main & fork
        pr = (
            await client.post(
                f"{self.url_main}/pulls",
                json={
                    "base": self.master_branch_name,
                    "head": f"{owner_name}:{branch_name1}",
                    "title": "squash the PR",
                    "body": "This is a squash_test",
                    "draft": False,
                },
            )
        ).json()

        await self.wait_for("pull_request", {"action": "opened"})

        commits = await self.get_commits(pr["number"])
        assert len(commits) > 1
        await self.run_engine()

        # do the squash
        await self.create_comment(pr["number"], "@mergifyio squash")
        await self.run_engine()

        # wait after the update & get the PR
        await self.wait_for("pull_request", {"action": "synchronize"})

        pr = await client.item(f"{self.url_main}/pulls/{pr['number']}")
        assert pr["commits"] == 1
