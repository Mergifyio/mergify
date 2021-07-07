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
import os

import yaml

from mergify_engine import context
from mergify_engine.tests.functional import base


class BackportActionTestBase(base.FunctionalTestBase):
    async def _do_test_backport(
        self,
        method,
        config=None,
        expected_title=None,
        expected_body=None,
        expected_author=None,
    ):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {"merge": {"method": method, "rebase_fallback": None}},
                },
                {
                    "name": "Backport to stable/#3.1",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {"backport": config or {"branches": [stable_branch]}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, commits = await self.create_pr(two_commits=True)

        # Create another PR to be sure we don't mess things up
        # see https://github.com/Mergifyio/mergify-engine/issues/849
        await self.create_pr(base=stable_branch)

        await self.add_label(p["number"], "backport-#3.1")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        pulls = await self.get_pulls(
            params={"state": "all", "base": self.master_branch_name}
        )
        assert 1 == len(pulls)
        assert "closed" == pulls[0]["state"]

        pulls = await self.get_pulls(params={"state": "all", "base": stable_branch})
        assert 2 == len(pulls)
        assert not await self.is_pull_merged(pulls[0]["number"])
        assert not await self.is_pull_merged(pulls[1]["number"])

        bp_pull = pulls[0]
        if expected_title is None:
            assert bp_pull["title"].endswith(
                f": pull request n1 from fork (backport #{p['number']})"
            )
        else:
            assert bp_pull["title"] == expected_title

        if expected_body is not None:
            assert bp_pull["body"].startswith(expected_body)

        if expected_author is not None:
            assert bp_pull["user"]["login"] == expected_author

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: Backport to stable/#3.1 (backport)"
        ]
        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            f"* [#%d %s](%s) has been created for branch `{stable_branch}`"
            % (
                bp_pull["number"],
                bp_pull["title"],
                bp_pull["html_url"],
            )
            == checks[0]["output"]["summary"]
        )

        refs = [
            ref["ref"]
            async for ref in self.find_git_refs(self.url_main, ["mergify/bp"])
        ]
        assert [f"refs/heads/mergify/bp/{stable_branch}/pr-{p['number']}"] == refs
        return await self.get_pull(pulls[0]["number"])


class TestBackportAction(BackportActionTestBase):
    async def test_backport_cancelled(self):
        stable_branch = self.get_full_branch_name("stable/3.1")
        rules = {
            "pull_request_rules": [
                {
                    "name": "backport",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-3.1",
                    ],
                    "actions": {"backport": {"branches": [stable_branch]}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()

        await self.add_label(p["number"], "backport-3.1")
        await self.run_engine()
        await self.remove_label(p["number"], "backport-3.1")
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: backport (backport)"
        ]
        assert "cancelled" == checks[0]["conclusion"]
        assert "The rule doesn't match anymore" == checks[0]["output"]["title"]

    async def test_backport_no_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {"merge": {"method": "merge", "rebase_fallback": None}},
                },
                {
                    "name": "Backport",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {"backport": {"branches": ["crashme"]}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules), test_branches=[])

        p, commits = await self.create_pr(two_commits=True)

        await self.add_label(p["number"], "backport-#3.1")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: Backport (backport)"
        ]
        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            "* Backport to branch `crashme` failed: Branch not found"
            == checks[0]["output"]["summary"]
        )

    async def _do_backport_conflicts(self, ignore_conflicts, labels=None):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "Backport to stable/#3.1",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "label=backport-#3.1",
                    ],
                    "actions": {
                        "backport": {
                            "branches": [stable_branch],
                            "ignore_conflicts": ignore_conflicts,
                        }
                    },
                },
            ]
        }
        if labels is not None:
            rules["pull_request_rules"][1]["actions"]["backport"]["labels"] = labels

        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        # Commit something in stable
        await self.git("checkout", "--quiet", stable_branch)
        # Write in the file that create_pr will create in master
        with open(os.path.join(self.git.tmp, "conflicts"), "wb") as f:
            f.write(b"conflicts incoming")
        await self.git("add", "conflicts")
        await self.git("commit", "--no-edit", "-m", "add conflict")
        await self.git("push", "--quiet", "main", stable_branch)

        p, commits = await self.create_pr(files={"conflicts": "ohoh"})

        await self.add_label(p["number"], "backport-#3.1")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        return (
            p,
            [
                c
                for c in await ctxt.pull_engine_check_runs
                if c["name"] == "Rule: Backport to stable/#3.1 (backport)"
            ],
        )

    async def test_backport_conflicts(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        p, checks = await self._do_backport_conflicts(False)

        # Retrieve the new commit id that has been be cherry-picked
        await self.git("fetch", "main")
        commit_id = (
            await self.git("show-ref", "--hash", f"main/{self.master_branch_name}")
        ).strip()

        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            f"""* Backport to branch `{stable_branch}` failed
Cherry-pick of {commit_id} has failed:
```
On branch mergify/bp/{stable_branch}/pr-{p['number']}
Your branch is up to date with 'origin/{stable_branch}'.

You are currently cherry-picking commit {commit_id[:7]}.
  (fix conflicts and run "git cherry-pick --continue")
  (use "git cherry-pick --skip" to skip this patch)
  (use "git cherry-pick --abort" to cancel the cherry-pick operation)

Unmerged paths:
  (use "git add <file>..." to mark resolution)
	both added:      conflicts

no changes added to commit (use "git add" and/or "git commit -a")
```


"""
            == checks[0]["output"]["summary"]
        )

    async def test_backport_ignore_conflicts(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        p, checks = await self._do_backport_conflicts(True, ["backported"])

        pull = (await self.get_pulls(params={"base": stable_branch}))[0]

        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            f"* [#%d %s](%s) has been created for branch `{stable_branch}`"
            % (
                pull["number"],
                pull["title"],
                pull["html_url"],
            )
            == checks[0]["output"]["summary"]
        )
        assert sorted(label["name"] for label in pull["labels"]) == [
            "backported",
            "conflicts",
        ]
        assert pull["assignees"] == []

    async def test_backport_with_labels(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        p = await self._do_test_backport(
            "merge", config={"branches": [stable_branch], "labels": ["backported"]}
        )
        assert [label["name"] for label in p["labels"]] == ["backported"]

    async def test_backport_merge_commit(self):
        p = await self._do_test_backport("merge")
        assert 2 == p["commits"]

    async def test_backport_merge_commit_regexes(self):
        prefix = self.get_full_branch_name("stable")
        p = await self._do_test_backport(
            "merge",
            config={"regexes": [f"^{prefix}/.*$"], "assignees": ["mergify-test3"]},
        )
        assert 2 == p["commits"]
        assert len(p["assignees"]) == 1
        assert p["assignees"][0]["login"] == "mergify-test3"

    async def test_backport_squash_and_merge(self):
        p = await self._do_test_backport("squash")
        assert 1 == p["commits"]

    async def test_backport_rebase_and_merge(self):
        p = await self._do_test_backport("rebase")
        assert 2 == p["commits"]

    async def test_backport_with_title_and_body(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        await self._do_test_backport(
            "merge",
            config={
                "branches": [stable_branch],
                "title": "foo: {{destination_branch}}",
                "body": "foo: {{destination_branch}}",
            },
            expected_title=f"foo: {stable_branch}",
            expected_body=f"foo: {stable_branch}",
        )


class TestBackportActionWithSub(BackportActionTestBase):
    SUBSCRIPTION_ACTIVE = True

    async def test_backport_with_bot_account(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        await self._do_test_backport(
            "merge",
            config={
                "branches": [stable_branch],
                "bot_account": "mergify-test3",
            },
            expected_author="mergify-test3",
        )
