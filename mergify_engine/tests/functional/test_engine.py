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
import unittest
from unittest import mock

import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import engine
from mergify_engine.clients import github
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


class TestEngineV2Scenario(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        with open(engine.mergify_rule_path, "r") as f:
            engine.MERGIFY_RULE = yaml.safe_load(
                f.read().replace("mergify[bot]", "mergify-test[bot]")
            )
        super(TestEngineV2Scenario, self).setUp()

    def test_invalid_configuration(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "foobar",
                    "wrong key": 123,
                },
            ],
        }
        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()

        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        assert check["output"]["summary"] == (
            "* extra keys not allowed @ data['pull_request_rules'][0]['wrong key']\n"
            "* required key not provided @ data['pull_request_rules'][0]['actions']\n"
            "* required key not provided @ data['pull_request_rules'][0]['conditions']"
        )

    @unittest.skip("FIXME: annotations are empty")
    def test_invalid_yaml_configuration(self):
        self.setup_repo("- this is totally invalid yaml\\n\n  - *\n*")
        p, _ = self.create_pr()

        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The Mergify configuration is invalid"
        # Use startswith because the message has some weird \x00 char
        assert check["output"]["summary"].startswith(
            """Invalid YAML at [line 3, column 2]
```
while scanning an alias
  in "<byte string>", line 3, column 1:
    *
    ^
expected alphabetic or numeric character, but found"""
        )
        check_id = check["id"]
        annotations = list(
            ctxt.client.items(
                f"{ctxt.base_url}/check-runs/{check_id}/annotations",
                api_version="antiope",
            )
        )
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

    def test_invalid_new_configuration(self):
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
        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr(files={".mergify.yml": "not valid"})

        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = ctxt.pull_engine_check_runs
        assert len(checks) == 1
        check = checks[0]
        assert check["output"]["title"] == "The new Mergify configuration is invalid"
        assert check["output"]["summary"] == "expected a dictionary"

    def test_backport_cancelled(self):
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

        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()

        self.add_label(p, "backport-3.1")
        self.run_engine()
        p.remove_from_labels("backport-3.1")
        self.wait_for("pull_request", {"action": "unlabeled"})
        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = list(
            c
            for c in ctxt.pull_engine_check_runs
            if c["name"] == "Rule: backport (backport)"
        )
        assert "cancelled" == checks[0]["conclusion"]
        assert "The rule doesn't match anymore" == checks[0]["output"]["title"]

    def test_backport_no_branch(self):
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

        self.setup_repo(yaml.dump(rules), test_branches=[])

        p, commits = self.create_pr(two_commits=True)

        self.add_label(p, "backport-#3.1")
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = list(
            c
            for c in ctxt.pull_engine_check_runs
            if c["name"] == "Rule: Backport (backport)"
        )
        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            "* Backport to branch `crashme` failed: Branch not found"
            == checks[0]["output"]["summary"]
        )

    def _do_backport_conflicts(self, ignore_conflicts):
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

        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        # Commit something in stable
        self.git("checkout", "--quiet", stable_branch)
        # Write in the file that create_pr will create in master
        with open(os.path.join(self.git.tmp, "conflicts"), "wb") as f:
            f.write(b"conflicts incoming")
        self.git("add", "conflicts")
        self.git("commit", "--no-edit", "-m", "add conflict")
        self.git("push", "--quiet", "main", stable_branch)

        p, commits = self.create_pr(files={"conflicts": "ohoh"})

        self.add_label(p, "backport-#3.1")
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        return (
            p,
            list(
                c
                for c in ctxt.pull_engine_check_runs
                if c["name"] == "Rule: Backport to stable/#3.1 (backport)"
            ),
        )

    def test_backport_conflicts(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        p, checks = self._do_backport_conflicts(False)

        # Retrieve the new commit id that has been be cherry-picked
        self.git("fetch", "main")
        commit_id = (
            self.git("show-ref", "--hash", f"main/{self.master_branch_name}")
            .decode("utf-8")
            .strip()
        )

        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            f"""* Backport to branch `{stable_branch}` failed


Cherry-pick of {commit_id} has failed:
```
On branch mergify/bp/{stable_branch}/pr-{p.number}
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

    def test_backport_ignore_conflicts(self):
        stable_branch = self.get_full_branch_name("stable/#3.1")
        p, checks = self._do_backport_conflicts(True)

        pull = list(self.r_o_admin.get_pulls(base=stable_branch))[0]

        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            f"* [#%d %s](%s) has been created for branch `{stable_branch}`"
            % (
                pull.number,
                pull.title,
                pull.html_url,
            )
            == checks[0]["output"]["summary"]
        )
        assert [label.name for label in pull.labels] == ["conflicts"]

    def _do_test_backport(self, method, config=None):
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

        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, commits = self.create_pr(two_commits=True)

        # Create another PR to be sure we don't mess things up
        # see https://github.com/Mergifyio/mergify-engine/issues/849
        self.create_pr(base=stable_branch)

        self.add_label(p, "backport-#3.1")
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is True
        assert "closed" == pulls[0].state

        pulls = list(self.r_o_admin.get_pulls(state="all", base=stable_branch))
        assert 2 == len(pulls)
        assert pulls[0].merged is False
        assert pulls[1].merged is False

        bp_pull = pulls[0]
        assert bp_pull.title == f"Pull request n1 from fork (bp #{p.number})"

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = list(
            c
            for c in ctxt.pull_engine_check_runs
            if c["name"] == "Rule: Backport to stable/#3.1 (backport)"
        )
        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            f"* [#%d %s](%s) has been created for branch `{stable_branch}`"
            % (
                bp_pull.number,
                bp_pull.title,
                bp_pull.html_url,
            )
            == checks[0]["output"]["summary"]
        )

        assert [f"mergify/bp/{stable_branch}/pr-{p.number}"] == [
            b.name
            for b in self.r_o_admin.get_branches()
            if b.name.startswith("mergify/bp")
        ]
        return pulls[0]

    def test_backport_merge_commit(self):
        p = self._do_test_backport("merge")
        assert 2 == p.commits

    def test_backport_merge_commit_regexes(self):
        prefix = self.get_full_branch_name("stable")
        p = self._do_test_backport("merge", config={"regexes": [f"^{prefix}/.*$"]})
        assert 2 == p.commits

    def test_backport_squash_and_merge(self):
        p = self._do_test_backport("squash")
        assert 1 == p.commits

    def test_backport_rebase_and_merge(self):
        p = self._do_test_backport("rebase")
        assert 2 == p.commits

    def test_merge_with_not_merged_attribute(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge on master",
                    "conditions": [f"base={self.master_branch_name}", "-merged"],
                    "actions": {"merge": {}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        p.update()
        self.assertEqual(True, p.merged)

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        for check in ctxt.pull_check_runs:
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
            assert False, "Merge check not found"

    def test_merge_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=squash"],
                    "actions": {"merge": {"method": "squash"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(files={"foo": "bar"})
        p2, _ = self.create_pr(two_commits=True)
        p1.merge()

        self.add_label(p2, "squash")

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        p2.update()
        assert 2 == p2.commits
        assert p2.merged is True

    def test_merge_strict_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [f"base={self.master_branch_name}", "label=squash"],
                    "actions": {"merge": {"strict": "smart", "method": "squash"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(files={"foo": "bar"})
        p2, _ = self.create_pr(two_commits=True)
        p1.merge()

        self.add_label(p2, "squash")
        self.run_engine()
        self.wait_for("pull_request", {"action": "synchronize"})

        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        p2.update()
        assert 3 == p2.commits
        assert p2.merged is True

    def test_merge_strict_rebase(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        assert 1 == len(commits2)
        assert commits[0].sha != commits2[0].sha
        assert commits[0].commit.message == commits2[0].commit.message

        # Retry to merge pr2
        self.create_status(p2)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        assert previous_master_sha != master_sha

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_merge_strict_rebase_with_user(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())
        events2 = list(p2.get_issue_events())

        assert 1 == len(commits2)
        assert commits[0].sha != commits2[0].sha
        assert commits[0].commit.message == commits2[0].commit.message
        assert events2[0].actor.login == "mergify-test1"

    def test_merge_strict_rebase_with_invalid_user(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.run_engine()

        self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = context.Context(self.cli_integration, p2.raw_data, {})
        checks = list(
            c
            for c in ctxt.pull_engine_check_runs
            if c["name"] == "Rule: smart strict merge on master (merge)"
        )
        assert checks[0]["output"]["title"] == "Base branch update has failed"
        assert checks[0]["output"]["summary"].startswith(
            "Unable to rebase: user `not-exists` is unknown. "
            "Please make sure `not-exists` has logged in Mergify dashboard"
        )

    def test_merge_strict_default(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1].commit.message
        )

        # Retry to merge pr2
        self.create_status(p2)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        assert previous_master_sha != master_sha

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_merge_smart_strict(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])
        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})
        self.run_engine()

        r = self.app.get(
            "/queues/%s" % (config.INSTALLATION_ID),
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        assert r.json() == {
            "mergifyio-testing/%s"
            % self.REPO_NAME: {self.master_branch_name: [p2.number]}
        }

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            in commits2[-1].commit.message
        )

        ctxt = context.Context(self.cli_integration, p2.raw_data, {})
        for check in ctxt.pull_check_runs:
            if check["name"] == "Rule: strict merge on master (merge)":
                assert (
                    "will be merged soon.\n\n"
                    "The following pull requests are queued:\n"
                    f"* #{p2.number} (priority: medium)\n\n"
                    "Required conditions for merge:\n\n"
                    f"- [X] `base={self.master_branch_name}`\n"
                    "- [ ] `status-success=continuous-integration/fake-ci`\n"
                    "- [X] `#approved-reviews-by>=1`"
                ) in check["output"]["summary"]
                break
        else:
            assert False, "Merge check not found"

        # Retry to merge pr2
        self.create_status(p2)
        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        assert previous_master_sha != master_sha

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_merge_failure_smart_strict(self):
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
        self.setup_repo(yaml.dump(rules), test_branches=[stable_branch])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()
        p3, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2, "continuous-integration/fake-ci", "success")
        self.run_engine()

        self.wait_for("pull_request", {"action": "synchronize"})

        self.create_status(p3, "continuous-integration/fake-ci", "success")
        self.run_engine()

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr2')}"
            == commits2[-1].commit.message
        )

        self.create_status(p2, "continuous-integration/fake-ci", "failure")
        self.run_engine()

        # FIXME(sileht): Previous actions tracker was posting a "Rule XXXX (merge)" with
        # neutral status saying the Merge doesn't match anymore, the new one doesn't
        # It's not a big deal as the rule doesn't match anymore anyway.
        self.wait_for("check_run", {"check_run": {"conclusion": "cancelled"}})

        # Should got to the next PR
        self.run_engine()
        self.wait_for("pull_request", {"action": "synchronize"})

        p3 = self.r_o_admin.get_pull(p3.number)
        commits3 = list(p3.get_commits())
        assert (
            f"Merge branch '{self.master_branch_name}' into {self.get_full_branch_name('fork/pr3')}"
            == commits3[-1].commit.message
        )

        self.create_status(p3, "continuous-integration/fake-ci", "success")
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        assert previous_master_sha != master_sha

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 1 == len(pulls)

    def test_short_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, commits = self.create_pr()

        client = github.get_client(p.base.user.login)
        pull = context.Context(client, p.raw_data, {})

        logins = pull.resolve_teams(
            ["user", "@testing", "@unknown/team", "@invalid/team/break-here"]
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "@unknown/team",
                "@invalid/team/break-here",
                "sileht",
                "jd",
                "mergify-test1",
                "mergify-test3",
            ]
        )

    def test_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        f"base={self.master_branch_name}",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, commits = self.create_pr()

        client = github.get_client(p.base.user.login)
        pull = context.Context(client, p.raw_data, {})

        logins = pull.resolve_teams(
            [
                "user",
                "@mergifyio-testing/testing",
                "@unknown/team",
                "@invalid/team/break-here",
            ]
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "@unknown/team",
                "@invalid/team/break-here",
                "jd",
                "sileht",
                "mergify-test1",
                "mergify-test3",
            ]
        )

    def _test_merge_custom_msg(
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

        self.setup_repo(yaml.dump(rules))

        if msg is None:
            msg = "This is the title\n\nAnd this is the message"
        p, _ = self.create_pr(message=f"It fixes it\n\n## {header}{msg}")
        self.create_status(p)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is True

        commit = self.r_o_admin.get_commits()[0].commit
        if commit_msg is None:
            commit_msg = msg
        assert commit_msg == commit.message

    def test_merge_custom_msg(self):
        return self._test_merge_custom_msg("Commit Message:\n")

    def test_merge_custom_msg_case(self):
        return self._test_merge_custom_msg("Commit message\n")

    def test_merge_custom_msg_rn(self):
        return self._test_merge_custom_msg("Commit Message\r\n")

    def test_merge_custom_msg_merge(self):
        return self._test_merge_custom_msg("Commit Message:\n", "merge")

    def test_merge_custom_msg_template(self):
        return self._test_merge_custom_msg(
            "Commit Message:\n",
            "merge",
            msg="{{title}}\n\nThanks to {{author}}",
            commit_msg="Pull request n1 from fork\n\nThanks to mergify-test2",
        )

    def test_merge_invalid_custom_msg(self):
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

        self.setup_repo(yaml.dump(rules))

        msg = "This is the title\n\nAnd this is the message {{invalid}}"
        p, _ = self.create_pr(message=f"It fixes it\n\n## Commit Message\n{msg}")
        self.create_status(p)

        self.run_engine()

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is False

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = list(
            c for c in ctxt.pull_engine_check_runs if c["name"] == "Rule: merge (merge)"
        )
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "action_required"
        assert (
            "Unknown pull request attribute: invalid" == checks[0]["output"]["summary"]
        )
        assert "Invalid commit message" == checks[0]["output"]["title"]

        # Edit and fixes the typo
        p.edit(body="It fixes it\n\n## Commit Message\n\nHere it is valid now")
        self.wait_for("pull_request", {"action": "edited"})

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for(
            "check_run",
            {"check_run": {"conclusion": "success", "status": "completed"}},
        )

        # delete check run cache
        del ctxt.__dict__["pull_check_runs"]
        checks = list(
            c for c in ctxt.pull_engine_check_runs if c["name"] == "Rule: merge (merge)"
        )
        assert "completed" == checks[0]["status"]
        assert checks[0]["conclusion"] == "success"
        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged

    def test_merge_custom_msg_title_body(self):
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

        self.setup_repo(yaml.dump(rules))

        msg = "It fixes it"
        p, _ = self.create_pr(message=msg)
        self.create_status(p)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is True

        commit = self.r_o_admin.get_commits()[0].commit
        assert f"Pull request n1 from fork (#{p.number})\n\n{msg}" == commit.message

    def test_merge_and_closes_issues(self):
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

        self.setup_repo(yaml.dump(rules))

        i = self.r_o_admin.create_issue(
            title="Such a bug", body="I can't explain, but don't work"
        )
        p, commits = self.create_pr(message="It fixes it\n\nCloses #%s" % i.number)
        self.create_status(p)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert p.number == pulls[0].number
        assert pulls[0].merged is True
        assert "closed" == pulls[0].state

        issue = self.r_o_admin.get_issue(i.number)
        assert "closed" == issue.state

    def test_rebase(self):
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

        self.setup_repo(yaml.dump(rules))

        p2, commits = self.create_pr()
        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is True
        assert "closed" == pulls[0].state

    def test_merge_branch_protection_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [f"base={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        rule = {
            "protection": {
                "required_status_checks": {
                    "strict": False,
                    "contexts": ["continuous-integration/fake-ci"],
                },
                "required_pull_request_reviews": None,
                "restrictions": None,
                "enforce_admins": False,
            }
        }

        self.branch_protection_protect(self.master_branch_name, rule)

        p, _ = self.create_pr()

        self.run_engine()

        self.wait_for(
            "check_run",
            {"check_run": {"conclusion": None, "status": "in_progress"}},
        )

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        checks = list(
            c for c in ctxt.pull_engine_check_runs if c["name"] == "Rule: merge (merge)"
        )
        assert checks[0]["conclusion"] is None
        assert "in_progress" == checks[0]["status"]
        assert (
            "Waiting for the branch protection required status checks to be validated"
            in checks[0]["output"]["title"]
        )

        self.create_status(p)

        self.run_engine()

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(
            self.r_o_admin.get_pulls(state="all", base=self.master_branch_name)
        )
        assert 1 == len(pulls)
        assert pulls[0].merged is True

    def test_merge_branch_protection_strict(self):
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

        self.setup_repo(yaml.dump(rules))

        # Check policy of that branch is the expected one
        rule = {
            "protection": {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["continuous-integration/fake-ci"],
                },
                "required_pull_request_reviews": None,
                "restrictions": None,
                "enforce_admins": False,
            }
        }

        p1, _ = self.create_pr()
        p2, _ = self.create_pr()

        p1.merge()

        self.branch_protection_protect(self.master_branch_name, rule)

        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})

        self.create_status(p2)

        self.run_engine()
        self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        ctxt = context.Context(self.cli_integration, p2.raw_data, {})
        checks = list(
            c for c in ctxt.pull_engine_check_runs if c["name"] == "Rule: merge (merge)"
        )
        assert "failure" == checks[0]["conclusion"]
        assert (
            "Branch protection setting 'strict' conflicts with Mergify configuration"
            == checks[0]["output"]["title"]
        )

    def _init_test_refresh(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()
        self.run_engine()

        rules = {
            "pull_request_rules": [
                {
                    "name": "automerge",
                    "conditions": ["label!=wip"],
                    "actions": {"merge": {}},
                }
            ]
        }

        self.git("checkout", self.master_branch_name)
        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(yaml.dump(rules))
        self.git("add", ".mergify.yml")
        self.git("commit", "--no-edit", "-m", "automerge everything")
        self.git("push", "--quiet", "main", self.master_branch_name)
        self.wait_for("push", {})

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 2 == len(pulls)
        return p1, p2

    def test_refresh_pull(self):
        p1, p2 = self._init_test_refresh()

        self.app.post(
            "/refresh/%s/pull/%s" % (p1.base.repo.full_name, p1.number),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )

        self.app.post(
            "/refresh/%s/pull/%s" % (p2.base.repo.full_name, p2.number),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_command_refresh(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()

        self.run_engine()

        ctxt = context.Context(self.cli_integration, p.raw_data, {})
        ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.SUCCESS,
                title="whatever",
                summary="erased",
            )
        )

        assert len(ctxt.pull_check_runs) == 1
        assert ctxt.pull_check_runs[0]["name"] == "Summary"
        completed_at = ctxt.pull_check_runs[0]["completed_at"]

        p.create_issue_comment("@mergifyio refresh")

        self.wait_for("issue_comment", {"action": "created"})
        self.run_engine()

        del ctxt.__dict__["pull_check_runs"]
        assert len(ctxt.pull_check_runs) == 1
        assert ctxt.pull_check_runs[0]["name"] == "Summary"
        assert completed_at != ctxt.pull_check_runs[0]["completed_at"]

        p.update()
        comments = list(p.get_issue_comments())
        assert (
            "**Command `refresh`: success**\n> **Pull request refreshed**\n> \n"
            == comments[-1].body
        )

    def test_refresh_branch(self):
        p1, p2 = self._init_test_refresh()

        self.app.post(
            "/refresh/%s/branch/master" % (p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("pull_request", {"action": "closed"})
        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_refresh_repo(self):
        p1, p2 = self._init_test_refresh()

        self.app.post(
            "/refresh/%s" % (p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("pull_request", {"action": "closed"})
        pulls = list(self.r_o_admin.get_pulls(base=self.master_branch_name))
        assert 0 == len(pulls)

    def test_change_mergify_yml(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": [f"base!={self.master_branch_name}"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p1, commits1 = self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        self.run_engine()
        ctxt = context.Context(self.cli_integration, p1.raw_data, {})
        assert len(ctxt.pull_check_runs) == 1
        assert ctxt.pull_check_runs[0]["name"] == "Summary"

    def test_marketplace_event(self):
        with mock.patch(
            "mergify_engine.subscription.Subscription.get_subscription"
        ) as get_sub:
            get_sub.return_value = self.subscription
            r = self.app.post(
                "/marketplace",
                headers={
                    "X-Hub-Signature": "sha1=whatever",
                    "Content-type": "application/json",
                },
                json={
                    "sender": {"login": "jd"},
                    "marketplace_purchase": {
                        "account": {
                            "login": "mergifyio-testing",
                            "type": "Organization",
                        }
                    },
                },
            )
        assert r.content == b"Event queued"
        assert r.status_code == 202

    def test_refresh_on_conflict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "It conflict!"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = self.create_pr(files={"TESTING": "p1"})
        p2, _ = self.create_pr(files={"TESTING": "p2"})
        p1.merge()

        self.run_engine()

        # Wait a bit than Github refresh the mergeable_state before running the
        # engine
        if base.RECORD:
            time.sleep(10)

        self.run_engine()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for(
            "issue_comment",
            {"action": "created", "comment": {"body": "It conflict!"}},
        )

    def test_requested_reviews(self):
        team = list(self.o_admin.get_teams())[0]
        team.set_repo_permission(self.r_o_admin, "push")

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
        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr()
        p1.create_review_request(reviewers=["sileht"])
        self.wait_for("pull_request", {"action": "review_requested"})
        self.run_engine()
        self.wait_for("issue_comment", {"action": "created"})

        p2, _ = self.create_pr()
        p2.create_review_request(team_reviewers=[team.slug])
        self.wait_for("pull_request", {"action": "review_requested"})
        self.run_engine()
        self.wait_for("issue_comment", {"action": "created"})

        assert "review-requested user" == list(p1.get_issue_comments())[0].body
        assert "review-requested team" == list(p2.get_issue_comments())[0].body

    def test_truncated_check_output(self):
        # not used anyhow
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        self.setup_repo(yaml.dump(rules))
        pr, commits = self.create_pr()
        self.run_engine()
        pull = context.Context(self.cli_integration, pr.raw_data, {})
        check = check_api.set_check_run(
            pull,
            "Test",
            check_api.Result(
                check_api.Conclusion.SUCCESS, title="bla", summary="a" * 70000
            ),
        )
        assert check["output"]["summary"] == ("a" * 65532 + "…")

    def test_pull_request_complete(self):
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        client = github.get_client(p.base.user.login)
        ctxt = context.Context(client, p.raw_data, {})
        assert p.number == ctxt.pull["number"]
        assert "open" == ctxt.pull["state"]
        assert "clean" == ctxt.pull["mergeable_state"]

    def test_pull_refreshed_after_config_change(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "default",
                    "conditions": ["base=other"],
                    "actions": {"comment": {"message": "it works"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr(files={"foo": "bar"})
        self.run_engine()

        rules["pull_request_rules"][0]["conditions"][
            0
        ] = f"base={self.master_branch_name}"
        p_config, _ = self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        p_config.merge()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("push", {})

        self.run_engine()
        self.wait_for("issue_comment", {"action": "created"})

        p.update()
        comments = list(p.get_issue_comments())
        assert "it works" == comments[-1].body
