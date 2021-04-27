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
import logging
import os.path
import time
from unittest import mock

import pytest
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine.clients import github
from mergify_engine.rules import live_resolvers
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestEngineV2Scenario(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    async def test_invalid_configuration(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "wrong key": 123,
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        assert check["output"]["summary"] == (
            "* extra keys not allowed @ pull_request_rules → item 0 → wrong key\n"
            "* required key not provided @ pull_request_rules → item 0 → actions\n"
            "* required key not provided @ pull_request_rules → item 0 → conditions"
        )

    async def test_invalid_yaml_configuration(self):
        await self.setup_repo("- this is totally invalid yaml\\n\n  - *\n*")
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        # Use startswith because the message has some weird \x00 char
        assert check["output"]["summary"].startswith(
            """Invalid YAML @ line 3, column 2
```
while scanning an alias
  in "<byte string>", line 3, column 1:
    *
    ^
expected alphabetic or numeric character, but found"""
        )
        check_id = check["id"]
        annotations = [
            annotation
            async for annotation in ctxt.client.items(
                f"{ctxt.base_url}/check-runs/{check_id}/annotations",
            )
        ]
        assert annotations == [
            {
                "path": ".mergify.yml",
                "blob_href": mock.ANY,
                "start_line": 3,
                "start_column": 2,
                "end_line": 3,
                "end_column": 2,
                "annotation_level": "failure",
                "title": "Invalid YAML",
                "message": mock.ANY,
                "raw_details": None,
            }
        ]

    async def test_invalid_new_configuration(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "conditions": ["branch=master"],
                    "actions": {
                        "comment": {"message": "hello"},
                    },
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr(files={".mergify.yml": "not valid"})

        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The new Mergify configuration is invalid"
        assert check["output"]["summary"] == "expected a dictionary"

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
        print(await ctxt.pull_check_runs)
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

        pull = (await self.get_pulls(base=stable_branch))[0]

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

    async def _do_test_backport(
        self, method, config=None, expected_title=None, expected_body=None
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

        pulls = await self.get_pulls(state="all", base=self.master_branch_name)
        assert 1 == len(pulls)
        assert "closed" == pulls[0]["state"]

        pulls = await self.get_pulls(state="all", base=stable_branch)
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

    async def test_merge_with_not_merged_attribute(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}", "-merged"],
                    "actions": {"merge": {}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr()
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        for check in await ctxt.pull_check_runs:
            if check["name"] == "Rule: merge on master (merge)":
                assert (
                    "The pull request has been merged automatically"
                    == check["output"]["title"]
                )
                assert (
                    f"The pull request has been merged automatically at *{ctxt.pull['merge_commit_sha']}*"
                    == check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Merge check not found")

    async def test_merge_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=squash"],
                    "actions": {"merge": {"method": "squash"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr(files={"foo": "bar"})
        p2, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(p1["number"])

        await self.add_label(p2["number"], "squash")

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p2["number"])

        p2 = await self.get_pull(p2["number"])
        assert 2 == p2["commits"]

    async def test_merge_strict_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=squash"],
                    "actions": {"merge": {"strict": "smart", "method": "squash"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr(files={"foo": "bar"})
        p2, _ = await self.create_pr(two_commits=True)
        await self.merge_pull(p1["number"])

        await self.add_label(p2["number"], "squash")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        p2 = await self.get_pull(p2["number"])
        assert 3 == p2["commits"]
        assert p2["merged"] is True

    async def test_merge_strict_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True, "strict_method": "rebase"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        commits2 = await self.get_commits(p2["number"])

        assert 1 == len(commits2)
        assert commits[0]["sha"] != commits2[0]["sha"]
        assert commits[0]["commit"]["message"] == commits2[0]["commit"]["message"]

        # Retry to merge pr2
        p2 = await self.get_pull(p2["number"])
        await self.create_status(p2)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        master_sha = (await self.get_head_commit())["sha"]
        assert previous_master_sha != master_sha

        pulls = await self.get_pulls(base=self.master_branch_name)
        assert 0 == len(pulls)

    async def test_merge_strict_rebase_with_user(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "merge": {
                            "strict": True,
                            "strict_method": "rebase",
                            "bot_account": "mergify-test1",
                        }
                    },
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        commits2 = await self.get_commits(p2["number"])
        events2 = [
            e
            async for e in self.client_admin.items(
                f"{self.url_main}/issues/{p2['number']}/events"
            )
        ]

        assert 1 == len(commits2)
        assert commits[0]["sha"] != commits2[0]["sha"]
        assert commits[0]["commit"]["message"] == commits2[0]["commit"]["message"]
        assert events2[0]["actor"]["login"] == "mergify-test1"

    async def test_merge_strict_rebase_with_invalid_user(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {
                        "merge": {
                            "strict": True,
                            "strict_method": "rebase",
                            "bot_account": "not-exists",
                        }
                    },
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: smart strict merge on master (merge)"
        ]
        assert checks[0]["output"]["title"] == "Base branch update has failed"
        assert checks[0]["output"]["summary"].startswith(
            "Unable to rebase: user `not-exists` is unknown. "
            "Please make sure `not-exists` has logged in Mergify dashboard"
        )

    async def test_merge_strict_default(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])

        # Check master have been merged into the PR
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1]["commit"]["message"]
        )

        # Retry to merge pr2
        await self.create_status(p2)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        master_sha = (await self.get_head_commit())["sha"]
        assert previous_master_sha != master_sha

        pulls = await self.get_pulls(base=self.master_branch_name)
        assert 0 == len(pulls)

    async def test_merge_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2)
        await self.create_review(p2["number"])
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})
        await self.run_engine()

        r = await self.app.get(
            f"/queues/{config.TESTING_ORGANIZATION_ID}",
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )

        assert r.json() == {
            f"{self.REPO_ID}": {self.master_branch_name: [p2["number"]]}
        }

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])

        # Check master have been merged into the PR
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1]["commit"]["message"]
        )

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        for check in await ctxt.pull_check_runs:
            if check["name"] == "Rule: strict merge on master (merge)":
                assert (
                    "The pull request is the 1st in the queue to be merged"
                    == check["output"]["title"]
                )
                print(check["output"]["summary"])
                assert (
                    f"""**Required conditions for merge:**

- [X] `base={self.master_branch_name}`
- [ ] `status-success=continuous-integration/fake-ci`
- [X] `#approved-reviews-by>=1`

**The following pull requests are queued:**
| | Pull request | Priority |
| ---: | :--- | :--- |
| 1 | test_merge_smart_strict: pull request n2 from fork #{p2['number']} | medium |

---
"""
                    in check["output"]["summary"]
                )
                break
        else:
            pytest.fail("Merge check not found")

        # Retry to merge pr2
        await self.create_status(p2)
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        master_sha = (await self.get_head_commit())["sha"]
        assert previous_master_sha != master_sha

        pulls = await self.get_pulls(base=self.master_branch_name)
        assert 0 == len(pulls)

    async def test_merge_failure_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        stable_branch = self.get_full_branch_name("stable/3.1")
        await self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = await self.create_pr()
        p2, commits = await self.create_pr()
        p3, commits = await self.create_pr()

        await self.merge_pull(p["number"])
        await self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = (await self.get_head_commit())["sha"]

        await self.create_status(p2, "continuous-integration/fake-ci", "success")
        await self.run_engine()

        await self.wait_for("pull_request", {"action": "synchronize"})

        await self.create_status(p3, "continuous-integration/fake-ci", "success")
        await self.run_engine()

        p2 = await self.get_pull(p2["number"])
        commits2 = await self.get_commits(p2["number"])
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            == commits2[-1]["commit"]["message"]
        )

        await self.create_status(p2, "continuous-integration/fake-ci", "failure")
        await self.run_engine()

        # FIXME(sileht): Previous actions tracker was posting a "Rule XXXX (merge)" with
        # neutral status saying the Merge doesn't match anymore, the new one doesn't
        # It's not a big deal as the rule doesn't match anymore anyway.
        await self.wait_for("check_run", {"check_run": {"conclusion": "cancelled"}})

        # Should got to the next PR
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "synchronize"})

        p3 = await self.get_pull(p3["number"])
        commits3 = await self.get_commits(p3["number"])
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr3')}"
            == commits3[-1]["commit"]["message"]
        )

        await self.create_status(p3, "continuous-integration/fake-ci", "success")
        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        master_sha = (await self.get_head_commit())["sha"]
        assert previous_master_sha != master_sha

        pulls = await self.get_pulls(base=self.master_branch_name)
        assert 1 == len(pulls)

    async def test_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "valid teams",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "short teams",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "not exists teams",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/noexists",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "invalid organization",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@another-org/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                },
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p, commits = await self.create_pr()
        await self.run_engine()

        client = github.aget_client(p["base"]["user"]["login"])
        installation = context.Installation(
            p["base"]["user"]["id"],
            p["base"]["user"]["login"],
            self.subscription,
            client,
            self.redis_cache,
        )
        repository = context.Repository(
            installation, p["base"]["repo"]["name"], p["base"]["repo"]["id"]
        )
        ctxt = await context.Context.create(repository, p, [])

        logins = await live_resolvers.teams(
            ctxt,
            [
                "user",
                "@mergifyio-testing/testing",
            ],
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "jd",
                "sileht",
                "mergify-test1",
                "mergify-test3",
            ]
        )

        logins = await live_resolvers.teams(
            ctxt,
            [
                "user",
                "@testing",
            ],
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "jd",
                "sileht",
                "mergify-test1",
                "mergify-test3",
            ]
        )

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(ctxt, ["@unknown/team"])

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(ctxt, ["@mergifyio-testing/not-exists"])

        with self.assertRaises(live_resolvers.LiveResolutionFailure):
            await live_resolvers.teams(ctxt, ["@invalid/team/break-here"])

        summary = [
            c for c in await ctxt.pull_engine_check_runs if c["name"] == "Summary"
        ][0]
        assert summary["output"]["title"] == "2 faulty rules and 2 potential rules"
        for message in (
            "Team `@mergifyio-testing/noexists` does not exist",
            "Team `@another-org/testing` is not part of the organization `mergifyio-testing`",
        ):
            assert message in summary["output"]["summary"]

    async def _test_merge_custom_msg(
        self, header, method="squash", msg=None, commit_msg=None
    ):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": method}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        if msg is None:
            msg = "This is the title\n\nAnd this is the message"
        p, _ = await self.create_pr(message=f"It fixes it\n\n## {header}{msg}")
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        commit = (await self.get_head_commit())["commit"]
        if commit_msg is None:
            commit_msg = msg
        assert commit_msg == commit["message"]

    async def test_merge_custom_msg(self):
        return await self._test_merge_custom_msg("Commit Message:\n")

    async def test_merge_custom_msg_case(self):
        return await self._test_merge_custom_msg("Commit message\n")

    async def test_merge_custom_msg_rn(self):
        return await self._test_merge_custom_msg("Commit Message\r\n")

    async def test_merge_custom_msg_merge(self):
        return await self._test_merge_custom_msg("Commit Message:\n", "merge")

    async def test_merge_custom_msg_template(self):
        return await self._test_merge_custom_msg(
            "Commit Message:\n",
            "merge",
            msg="{{title}}\n\nThanks to {{author}}",
            commit_msg="test_merge_custom_msg_template: pull request n1 from fork\n\nThanks to mergify-test2",
        )

    async def test_merge_invalid_custom_msg(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": "merge"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        msg = "This is the title\n\nAnd this is the message {{invalid}}"
        p, _ = await self.create_pr(message=f"It fixes it\n\n## Commit Message\n{msg}")
        await self.create_status(p)

        await self.run_engine()

        assert not await self.is_pull_merged(p["number"])

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "action_required"
        assert (
            "Unknown pull request attribute: invalid" == checks[0]["output"]["summary"]
        )
        assert "Invalid commit message" == checks[0]["output"]["title"]

        # Edit and fixes the typo
        await self.edit_pull(
            p["number"], body="It fixes it\n\n## Commit Message\n\nHere it is valid now"
        )
        await self.wait_for("pull_request", {"action": "edited"})

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for(
            "check_run",
            {"check_run": {"conclusion": "success", "status": "completed"}},
        )

        # delete check run cache
        del ctxt._cache["pull_check_runs"]
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "success"

        assert await self.is_pull_merged(p["number"])

    async def test_merge_custom_msg_title_body(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {
                        "merge": {"method": "merge", "commit_message": "title+body"}
                    },
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        msg = "It fixes it"
        p, _ = await self.create_pr(message=msg)
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        commit = (await self.get_head_commit())["commit"]
        assert (
            f"test_merge_custom_msg_title_body: pull request n1 from fork (#{p['number']})\n\n{msg}"
            == commit["message"]
        )

    async def test_merge_and_closes_issues(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": "merge"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        i = await self.create_issue(
            title="Such a bug", body="I can't explain, but don't work"
        )
        p, commits = await self.create_pr(
            message=f"It fixes it\n\nCloses #{i['number']}"
        )
        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

        pulls = await self.get_pulls(state="all", base=self.master_branch_name)
        assert 1 == len(pulls)
        assert p["number"] == pulls[0]["number"]
        assert "closed" == pulls[0]["state"]

        issue = await self.client_admin.item(f"{self.url_main}/issues/{i['number']}")
        assert "closed" == issue["state"]

    async def test_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        p2, commits = await self.create_pr()
        await self.create_status(p2)
        await self.create_review(p2["number"])

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p2["number"])

    async def test_merge_branch_protection_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": False,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.master_branch_name, protection)

        p, _ = await self.create_pr()

        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": "success",
                    "status": "completed",
                    "name": "Summary",
                }
            },
        )

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert checks == []
        summary = [
            c for c in await ctxt.pull_engine_check_runs if c["name"] == "Summary"
        ][0]
        assert (
            f"""- [X] `base={self.master_branch_name}`
- [ ] `check-success=continuous-integration/fake-ci` (merge action only, due to branch protection)
"""
            in summary["output"]["summary"]
        )

        await self.create_status(p)

        await self.run_engine()

        await self.wait_for("pull_request", {"action": "closed"})

        assert await self.is_pull_merged(p["number"])

    async def test_merge_branch_protection_strict_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {"strict": True}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        await self.branch_protection_protect(self.master_branch_name, protection)

        p, _ = await self.create_pr()

        p2, _ = await self.create_pr()
        await self.create_status(p2)
        await self.merge_pull(p2["number"])

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.create_status(p)
        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": None,
                    "status": "in_progress",
                    "name": "Rule: merge (merge)",
                }
            },
        )

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert checks[0]["conclusion"] is None
        assert "The pull request will be merged soon" == checks[0]["output"]["title"]

        # Ensure it have been  rebased
        head_sha = p["head"]["sha"]
        p = await self.get_pull(p["number"])
        assert p["head"]["sha"] != head_sha

        # After rebase CI fail and PR is removed from queue
        await self.create_status(p, state="failure")
        await self.run_engine()

        await self.wait_for(
            "check_run",
            {
                "check_run": {
                    "conclusion": "success",
                    "status": "completed",
                    "name": "Summary",
                }
            },
        )
        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "cancelled" == checks[0]["conclusion"]
        assert "The rule doesn't match anymore" == checks[0]["output"]["title"]
        assert p["merged"] is False

        # CI is back, check PR is merged
        await self.create_status(p)
        await self.run_engine()

        p = await self.get_pull(p["number"])
        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "success" == checks[0]["conclusion"]
        assert (
            "The pull request has been merged automatically"
            == checks[0]["output"]["title"]
        )
        assert p["merged"] is True

    async def test_merge_branch_protection_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {}},
                }
            ]
        }

        await self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": ["continuous-integration/fake-ci"],
            },
            "required_pull_request_reviews": None,
            "restrictions": None,
            "enforce_admins": False,
        }

        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        await self.merge_pull(p1["number"])

        await self.branch_protection_protect(self.master_branch_name, protection)

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})

        await self.create_status(p2)

        await self.run_engine()
        await self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = await context.Context.create(self.repository_ctxt, p2, [])
        checks = [
            c
            for c in await ctxt.pull_engine_check_runs
            if c["name"] == "Rule: merge (merge)"
        ]
        assert "failure" == checks[0]["conclusion"]
        assert (
            "Branch protection setting 'strict' conflicts with Mergify configuration"
            == checks[0]["output"]["title"]
        )

    async def test_refresh_via_check_suite_rerequest(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, _ = await self.create_pr()

        await self.run_engine()

        ctxt = await self.repository_ctxt.get_pull_request_context(p["number"], p)
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check_suite_id = checks[0]["check_suite"]["id"]

        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}"
            )
        ).json()
        assert check_suite["status"] == "completed"

        # Click on rerequest btn
        await self.installation_ctxt.client.post(
            f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}/rerequest"
        )
        await self.wait_for("check_suite", {"action": "rerequested"})

        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}"
            )
        ).json()
        assert check_suite["status"] == "queued"

        await self.run_engine()
        check_suite = (
            await self.installation_ctxt.client.get(
                f"{self.repository_ctxt.base_url}/check-suites/{check_suite_id}"
            )
        ).json()
        assert check_suite["status"] == "completed"

    async def test_refresh_api(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p1, _ = await self.create_pr()
        p2, _ = await self.create_pr()

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}/pull/{p1['number']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p2['base']['repo']['full_name']}/pull/{p2['number']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}/branch/master",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

        resp = await self.app.post(
            f"/refresh/{p1['base']['repo']['full_name']}",
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        assert resp.status_code == 202, resp.text

    async def test_command_refresh(self) -> None:
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        p, commits = await self.create_pr()

        await self.run_engine()

        ctxt = await context.Context.create(
            self.repository_ctxt,
            p,
        )
        await (
            ctxt.set_summary_check(
                check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    title="whatever",
                    summary="erased",
                )
            )
        )

        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        completed_at = checks[0]["completed_at"]

        await self.create_comment(p["number"], "@mergifyio refresh")

        await self.run_engine()

        del ctxt._cache["pull_check_runs"]
        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert completed_at != checks[0]["completed_at"]

        comments = await self.get_issue_comments(p["number"])
        assert (
            "**Command `refresh`: success**\n> **Pull request refreshed**\n> \n"
            == comments[-1]["body"]
        )

    async def test_change_mergify_yml(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p1, commits1 = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.run_engine()
        ctxt = await context.Context.create(self.repository_ctxt, p1, [])
        checks = await ctxt.pull_check_runs
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert checks[0]["output"]["title"] == "The new Mergify configuration is valid"

    async def test_marketplace_event(self):
        with mock.patch(
            "mergify_engine.subscription.Subscription.get_subscription"
        ) as get_sub:
            get_sub.return_value = self.subscription
            r = await self.app.post(
                "/marketplace",
                headers={
                    "X-Hub-Signature": "sha1=whatever",
                    "Content-type": "application/json",
                },
                json={
                    "sender": {"login": "jd"},
                    "marketplace_purchase": {
                        "account": {
                            "id": 12345,
                            "login": "mergifyio-testing",
                            "type": "Organization",
                        }
                    },
                },
            )
        assert r.content == b"Event queued"
        assert r.status_code == 202

    async def test_refresh_on_conflict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "It conflict!"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = await self.create_pr(files={"TESTING": "p1"})
        p2, _ = await self.create_pr(files={"TESTING": "p2"})
        await self.merge_pull(p1["number"])

        await self.run_engine()

        # Wait a bit than Github refresh the mergeable_state before running the
        # engine
        if base.RECORD:
            time.sleep(10)

        await self.run_engine()
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for(
            "issue_comment",
            {"action": "created", "comment": {"body": "It conflict!"}},
        )

    async def test_requested_reviews(self):
        team = (await self.get_teams())[0]
        await self.add_team_permission(team["slug"], "push")

        rules = {
            "pull_request_rules": [
                {
                    "name": "user",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "review-requested=sileht",
                    ],
                    "actions": {"comment": {"message": "review-requested user"}},
                },
                {
                    "name": "team",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "review-requested=@testing",
                    ],
                    "actions": {"comment": {"message": "review-requested team"}},
                },
            ],
        }
        await self.setup_repo(yaml.dump(rules))

        p1, _ = await self.create_pr()
        await self.create_review_request(p1["number"], reviewers=["sileht"])
        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        # FIXME(sileht): This doesn't work anymore MRGFY-227
        # p2, _ = await self.create_pr()
        # p2.create_review_request(team_reviewers=[team.slug])
        # await self.wait_for("pull_request", {"action": "review_requested"})
        # await self.run_engine()
        # await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p1["number"])
        assert "review-requested user" == comments[0]["body"]

        # assert "review-requested team" == list(p2.get_issue_comments())[0].body

    async def test_truncated_check_output(self):
        # not used anyhow
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        await self.setup_repo(yaml.dump(rules))
        pr, commits = await self.create_pr()
        await self.run_engine()
        pull = await context.Context.create(self.repository_ctxt, pr, [])
        check = await check_api.set_check_run(
            pull,
            "Test",
            check_api.Result(
                check_api.Conclusion.SUCCESS, title="bla", summary="a" * 70000
            ),
        )
        assert check["output"]["summary"] == ("a" * 65532 + "…")

    async def test_pull_request_init_summary(self):
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        await self.setup_repo(yaml.dump(rules))

        # Run the engine once, to initialiaze the config location cache
        await self.create_pr()
        await self.run_engine()

        # Check initial summary is submitted
        p, _ = await self.create_pr()
        ctxt = await self.repository_ctxt.get_pull_request_context(p["number"], p)
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["output"]["title"] == "Your rules are under evaluation"
        assert (
            checks[0]["output"]["summary"]
            == "Be patient, the page will be updated soon."
        )

    async def test_pull_refreshed_after_config_change(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "default",
                    "conditions": ["base=other"],
                    "actions": {"comment": {"message": "it works"}},
                }
            ]
        }
        await self.setup_repo(yaml.dump(rules))

        p, _ = await self.create_pr(files={"foo": "bar"})
        await self.run_engine()

        rules["pull_request_rules"][0]["conditions"][
            0
        ] = f"base={self.master_branch_name}"
        p_config, _ = await self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        await self.merge_pull(p_config["number"])
        await self.wait_for("pull_request", {"action": "closed"})
        await self.wait_for("push", {})

        await self.run_engine()
        await self.wait_for("issue_comment", {"action": "created"})

        comments = await self.get_issue_comments(p["number"])
        assert "it works" == comments[-1]["body"]

    async def test_unconfigured_repo_does_not_post_summary(self):
        await self.setup_repo()

        await self.run_engine()
        p, _ = await self.create_pr()
        await self.run_engine()

        ctxt = await context.Context.create(self.repository_ctxt, p, [])
        assert await ctxt.pull_engine_check_runs == []

    async def test_check_run_api(self):
        await self.setup_repo()
        p, _ = await self.create_pr()
        ctxt = await context.Context.create(self.repository_ctxt, p, [])

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.PENDING, title="PENDING", summary="PENDING"
            ),
        )
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "in_progress"
        assert checks[0]["conclusion"] is None
        assert checks[0]["completed_at"] is None
        assert checks[0]["output"]["title"] == "PENDING"
        assert checks[0]["output"]["summary"] == "PENDING"

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.CANCELLED, title="CANCELLED", summary="CANCELLED"
            ),
        )
        ctxt._cache = {}
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "completed"
        assert checks[0]["conclusion"] == "cancelled"
        assert checks[0]["completed_at"] is not None
        assert checks[0]["output"]["title"] == "CANCELLED"
        assert checks[0]["output"]["summary"] == "CANCELLED"

        await check_api.set_check_run(
            ctxt,
            "Test",
            check_api.Result(
                check_api.Conclusion.PENDING, title="PENDING", summary="PENDING"
            ),
        )
        ctxt._cache = {}
        checks = await ctxt.pull_engine_check_runs
        assert len(checks) == 1
        assert checks[0]["status"] == "in_progress"
        assert checks[0]["conclusion"] is None
        assert checks[0]["completed_at"] is None
        assert checks[0]["output"]["title"] == "PENDING"
        assert checks[0]["output"]["summary"] == "PENDING"

    async def test_get_repository_by_id(self):
        repo = await self.installation_ctxt.get_repository_by_id(self.REPO_ID)
        assert repo.name == self.REPO_NAME
        assert repo.name == self.repository_ctxt.name
