# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import debug
from mergify_engine import mergify_pull
from mergify_engine.clients import github
from mergify_engine.tasks import engine
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)


def run_smart_strict_workflow_periodic_task():
    # NOTE(sileht): actions must not be loaded manually before the celery
    # worker. Otherwise we have circular import loop.
    from mergify_engine.actions.merge import queue

    queue.smart_strict_workflow_periodic_task.apply_async()


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

    def test_backport_cancelled(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "backport",
                    "conditions": ["base=master", "label=backport-3.1"],
                    "actions": {"backport": {"branches": ["stable/3.1"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/3.1"])

        p, _ = self.create_pr()

        self.add_label(p, "backport-3.1")
        p.remove_from_labels("backport-3.1")
        self.wait_for("pull_request", {"action": "unlabeled"})

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        checks = list(
            check_api.get_checks(pull, check_name="Rule: backport (backport)")
        )
        self.assertEqual("neutral", checks[0]["conclusion"])
        self.assertEqual(
            "The rule doesn't match anymore, this action has been cancelled",
            checks[0]["output"]["title"],
        )

    def test_delete_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": None},
                },
                {
                    "name": "delete on close",
                    "conditions": ["base=master", "label=close", "closed"],
                    "actions": {"delete_head_branch": {}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p1.merge()
        self.wait_for("pull_request", {"action": "closed"})

        p2, _ = self.create_pr(base_repo="main", branch="#2-second-pr")
        p2.edit(state="close")
        self.wait_for("pull_request", {"action": "closed"})

        self.add_label(p1, "merge")
        self.add_label(p2, "close")

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(2, len(pulls))

        branches = list(self.r_o_admin.get_branches())
        self.assertEqual(1, len(branches))
        self.assertEqual("master", branches[0].name)

    def test_delete_branch_with_dep_no_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": None},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p2, _ = self.create_pr(
            base_repo="main", branch="#2-second-pr", base="#1-first-pr"
        )

        p1.merge()
        self.wait_for("pull_request", {"action": "closed"})
        self.add_label(p1, "merge")
        self.wait_for("check_run", {"check_run": {"conclusion": "success"}})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        assert 2 == len(pulls)

        branches = list(self.r_o_admin.get_branches())
        assert 3 == len(branches)
        assert {"master", "#1-first-pr", "#2-second-pr"} == {b.name for b in branches}

    def test_delete_branch_with_dep_force(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "delete on merge",
                    "conditions": ["base=master", "label=merge", "merged"],
                    "actions": {"delete_head_branch": {"force": True}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(base_repo="main", branch="#1-first-pr")
        p2, _ = self.create_pr(
            base_repo="main", branch="#2-second-pr", base="#1-first-pr"
        )

        p1.merge()
        self.wait_for("pull_request", {"action": "closed", "number": p1.number})
        self.add_label(p1, "merge")
        self.wait_for("pull_request", {"action": "closed", "number": p2.number})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        assert 2 == len(pulls)

        branches = list(self.r_o_admin.get_branches())
        assert 2 == len(branches)
        assert {"master", "#2-second-pr"} == {b.name for b in branches}

    def test_assign(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "assign",
                    "conditions": ["base=master"],
                    "actions": {"assign": {"users": ["mergify-test1"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["mergify-test1"]), sorted([l.login for l in pulls[0].assignees])
        )

    def test_request_reviews_users(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": ["base=master"],
                    "actions": {"request_reviews": {"users": ["mergify-test1"]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        requests = pulls[0].get_review_requests()
        self.assertEqual(
            sorted(["mergify-test1"]), sorted([l.login for l in requests[0]])
        )

    def test_request_reviews_teams(self):
        # Add a team to the repo with write permissions  so it can review
        team = list(self.o_admin.get_teams())[0]
        team.set_repo_permission(self.r_o_admin, "push")

        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": ["base=master"],
                    "actions": {"request_reviews": {"teams": [team.slug]}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        requests = pulls[0].get_review_requests()
        self.assertEqual(sorted([team.slug]), sorted([l.slug for l in requests[1]]))

    def test_debugger(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": ["base=master"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        debug.report(p.html_url)

    def test_review(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "approve",
                    "conditions": ["base=master"],
                    "actions": {"review": {"type": "APPROVE"}},
                },
                {
                    "name": "requested",
                    "conditions": ["base=master", "#approved-reviews-by>=1"],
                    "actions": {
                        "review": {"message": "WTF?", "type": "REQUEST_CHANGES"}
                    },
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        self.wait_for("pull_request_review", {}),

        p.update()
        comments = list(p.get_reviews())
        self.assertEqual(2, len(comments))
        self.assertEqual("APPROVED", comments[-2].state)
        self.assertEqual("CHANGES_REQUESTED", comments[-1].state)
        self.assertEqual("WTF?", comments[-1].body)

    def test_comment(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": ["base=master"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        p.update()
        comments = list(p.get_issue_comments())
        self.assertEqual("WTF?", comments[-1].body)

        # Add a label to trigger mergify
        self.add_label(p, "stable")

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)

        # Add new commit to ensure Summary get copied and comment not reposted
        open(self.git.tmp + "/new_file", "wb").close()
        self.git("add", self.git.tmp + "/new_file")
        self.git("commit", "--no-edit", "-m", "new commit")
        self.git("push", "--quiet", "fork", "fork/pr%d" % self.pr_counter)

        self.wait_for("pull_request", {"action": "synchronize"})

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)

    def test_comment_backwardcompat(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "comment",
                    "conditions": ["base=master"],
                    "actions": {"comment": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        p.update()
        comments = list(p.get_issue_comments())
        self.assertEqual("WTF?", comments[-1].body)

        # Override Summary with the old format
        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        check_api.set_check_run(
            pull,
            "Summary",
            "completed",
            "success",
            output={"title": "whatever", "summary": "erased"},
        )

        # Add a label to trigger mergify
        self.add_label(p, "stable")

        # Ensure nothing changed
        new_comments = list(p.get_issue_comments())
        self.assertEqual(len(comments), len(new_comments))
        self.assertEqual("WTF?", new_comments[-1].body)

    def test_close(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": ["base=master"],
                    "actions": {"close": {"message": "WTF?"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        p.update()
        self.assertEqual("closed", p.state)
        self.assertEqual("WTF?", list(p.get_issue_comments())[-1].body)

    def test_dismiss_reviews(self):
        return self._test_dismiss_reviews()

    def test_dismiss_reviews_custom_message(self):
        return self._test_dismiss_reviews(message="Loser")

    def _test_dismiss_reviews(self, message=None):
        rules = {
            "pull_request_rules": [
                {
                    "name": "dismiss reviews",
                    "conditions": ["base=master"],
                    "actions": {
                        "dismiss_reviews": {
                            "approved": True,
                            "changes_requested": ["mergify-test1"],
                        }
                    },
                }
            ]
        }

        if message is not None:
            rules["pull_request_rules"][0]["actions"]["dismiss_reviews"][
                "message"
            ] = message

        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()
        branch = "fork/pr%d" % self.pr_counter
        self.create_review(p, commits[-1], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        open(self.git.tmp + "/unwanted_changes", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes")
        self.git("commit", "--no-edit", "-m", "unwanted_changes")
        self.git("push", "--quiet", "fork", branch)

        self.wait_for("pull_request", {"action": "synchronize"})
        self.wait_for("pull_request_review", {"action": "dismissed"})

        self.assertEqual(
            [("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        commits = list(p.get_commits())
        self.create_review(p, commits[-1], "REQUEST_CHANGES")

        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("CHANGES_REQUESTED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        open(self.git.tmp + "/unwanted_changes2", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes2")
        self.git("commit", "--no-edit", "-m", "unwanted_changes2")
        self.git("push", "--quiet", "fork", branch)

        self.wait_for("pull_request", {"action": "synchronize"})
        self.wait_for("pull_request_review", {"action": "dismissed"})

        # There's no way to retrieve the dismiss message :(
        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

    def test_backport_no_branch(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {"merge": {"method": "merge", "rebase_fallback": None}},
                },
                {
                    "name": "Backport",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {"backport": {"branches": ["crashme"]}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=[])

        p, commits = self.create_pr(two_commits=True)

        self.add_label(p, "backport-#3.1")
        self.wait_for("pull_request", {"action": "closed"})

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        checks = list(
            check_api.get_checks(pull, check_name="Rule: Backport (backport)")
        )
        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            "* Backport to branch `crashme` failed: Branch not found" % ()
            == checks[0]["output"]["summary"]
        )

    def _do_backport_conflicts(self, ignore_conflicts):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {"merge": {"method": "rebase"}},
                },
                {
                    "name": "Backport to stable/#3.1",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {
                        "backport": {
                            "branches": ["stable/#3.1"],
                            "ignore_conflicts": ignore_conflicts,
                        }
                    },
                },
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/#3.1"])

        # Commit something in stable
        self.git("checkout", "--quiet", "stable/#3.1")
        # Write in the file that create_pr will create in master
        with open(os.path.join(self.git.tmp, "conflicts"), "wb") as f:
            f.write(b"conflicts incoming")
        self.git("add", "conflicts")
        self.git("commit", "--no-edit", "-m", "add conflict")
        self.git("push", "--quiet", "main", "stable/#3.1")

        p, commits = self.create_pr(files={"conflicts": "ohoh"})

        self.add_label(p, "backport-#3.1")
        self.wait_for("pull_request", {"action": "closed"})

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        return list(
            check_api.get_checks(
                pull, check_name="Rule: Backport to stable/#3.1 (backport)"
            )
        )

    def test_backport_conflicts(self):
        checks = self._do_backport_conflicts(False)

        # Retrieve the new commit id that has been be cherry-picked
        self.git("fetch", "main")
        commit_id = (
            self.git("show-ref", "--hash", "main/master").decode("utf-8").strip()
        )

        assert "failure" == checks[0]["conclusion"]
        assert "No backport have been created" == checks[0]["output"]["title"]
        assert (
            f"""* Backport to branch `stable/#3.1` failed


Cherry-pick of {commit_id} has failed:
```
On branch mergify/bp/stable/#3.1/pr-1
Your branch is up to date with 'origin/stable/#3.1'.

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
        checks = self._do_backport_conflicts(True)

        pull = list(self.r_o_admin.get_pulls())[0]

        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            "* [#%d %s](%s) has been created for branch `stable/#3.1`"
            % (pull.number, pull.title, pull.html_url,)
            == checks[0]["output"]["summary"]
        )
        assert [l.name for l in pull.labels] == ["conflicts"]

    def _do_test_backport(self, method, config=None):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {"merge": {"method": method, "rebase_fallback": None}},
                },
                {
                    "name": "Backport to stable/#3.1",
                    "conditions": ["base=master", "label=backport-#3.1"],
                    "actions": {"backport": config or {"branches": ["stable/#3.1"]}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/#3.1"])

        p, commits = self.create_pr(two_commits=True)

        self.add_label(p, "backport-#3.1")
        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].number)
        self.assertEqual(1, pulls[1].number)
        self.assertEqual(True, pulls[1].merged)
        self.assertEqual("closed", pulls[1].state)
        self.assertEqual(False, pulls[0].merged)

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        checks = list(
            check_api.get_checks(
                pull, check_name="Rule: Backport to stable/#3.1 (backport)"
            )
        )
        assert "success" == checks[0]["conclusion"]
        assert "Backports have been created" == checks[0]["output"]["title"]
        assert (
            "* [#%d %s](%s) has been created for branch `stable/#3.1`"
            % (pulls[0].number, pulls[0].title, pulls[0].html_url,)
            == checks[0]["output"]["summary"]
        )

        self.assertEqual(
            ["mergify/bp/stable/#3.1/pr-1"],
            [
                b.name
                for b in self.r_o_admin.get_branches()
                if b.name.startswith("mergify/bp")
            ],
        )
        return pulls[0]

    def test_backport_merge_commit(self):
        p = self._do_test_backport("merge")
        self.assertEquals(2, p.commits)

    def test_backport_merge_commit_regexes(self):
        p = self._do_test_backport("merge", config={"regexes": ["^stable/.*$"]})
        self.assertEquals(2, p.commits)

    def test_backport_squash_and_merge(self):
        p = self._do_test_backport("squash")
        self.assertEquals(1, p.commits)

    def test_backport_rebase_and_merge(self):
        p = self._do_test_backport("rebase")
        self.assertEquals(2, p.commits)

    def test_merge_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": ["base=master", "label=squash"],
                    "actions": {"merge": {"method": "squash"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(files={"foo": "bar"})
        p2, _ = self.create_pr(two_commits=True)
        p1.merge()

        self.add_label(p2, "squash")

        self.wait_for("pull_request", {"action": "closed"})

        p2.update()
        self.assertEqual(2, p2.commits)
        self.assertEqual(True, p2.merged)

    def test_merge_strict_squash(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": ["base=master", "label=squash"],
                    "actions": {"merge": {"strict": "smart", "method": "squash"}},
                },
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(files={"foo": "bar"})
        p2, _ = self.create_pr(two_commits=True)
        p1.merge()

        self.add_label(p2, "squash")

        run_smart_strict_workflow_periodic_task()

        self.wait_for("pull_request", {"action": "closed"})

        p2.update()
        self.assertEqual(3, p2.commits)
        self.assertEqual(True, p2.merged)

    def test_merge_strict_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True, "strict_method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/3.1"])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"}),

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        self.assertEquals(1, len(commits2))
        self.assertNotEqual(commits[0].sha, commits2[0].sha)
        self.assertEqual(commits[0].commit.message, commits2[0].commit.message)

        # Retry to merge pr2
        self.create_status(p2)

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_merge_strict_default(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "smart strict merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": True}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/3.1"])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        # Retry to merge pr2
        self.create_status(p2)

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_merge_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                        "#approved-reviews-by>=1",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/3.1"])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2)
        self.create_review(p2, commits[0])

        r = self.app.get(
            "/queues/%s" % (config.INSTALLATION_ID),
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        self.assertEqual(r.json, {"mergifyio-testing/%s" % self.name: {"master": [2]}})

        # We can run celery beat inside tests, so run the task manually
        run_smart_strict_workflow_periodic_task()

        r = self.app.get(
            "/queues/%s" % (config.INSTALLATION_ID),
            headers={
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
        )
        self.assertEqual(r.json, {"mergifyio-testing/%s" % self.name: {"master": [2]}})

        self.wait_for("pull_request", {"action": "synchronize"})

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        pull = mergify_pull.MergifyPull(self.cli_integration, p2.raw_data)
        checks = list(check_api.get_checks(pull))
        for check in checks:
            if check["name"] == "Rule: strict merge on master (merge)":
                assert (
                    "will be merged soon.\n\nThe following pull requests are queued: #2"
                    in check["output"]["summary"]
                )
                break
        else:
            assert False, "Merge check not found"

        # Retry to merge pr2
        self.create_status(p2)

        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_merge_failure_smart_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "strict merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"strict": "smart"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/3.1"])

        p, _ = self.create_pr()
        p2, commits = self.create_pr()
        p3, commits = self.create_pr()

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2, "continuous-integration/fake-ci", "success")

        # We can run celery beat inside tests, so run the task manually
        run_smart_strict_workflow_periodic_task()

        self.wait_for("pull_request", {"action": "synchronize"})

        self.create_status(p3, "continuous-integration/fake-ci", "success")

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        self.create_status(p2, "continuous-integration/fake-ci", "failure")

        # FIXME(sileht): Previous actions tracker was posting a "Rule XXXX (merge)" with
        # neutral status saying the Merge doesn't match anymore, the new one doesn't
        # It's not a big deal as the the rule doesn't match anymore anyways.:w
        self.wait_for("check_run", {"check_run": {"conclusion": "neutral"}})

        # Should got to the next PR
        run_smart_strict_workflow_periodic_task()

        self.wait_for("pull_request", {"action": "synchronize"})

        p3 = self.r_o_admin.get_pull(p3.number)
        commits3 = list(p3.get_commits())
        self.assertIn("Merge branch 'master' into fork/pr", commits3[-1].commit.message)

        self.create_status(p3, "continuous-integration/fake-ci", "success")
        self.wait_for("pull_request", {"action": "closed"})

        master_sha = self.r_o_admin.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))

    def test_short_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, commits = self.create_pr()

        client = github.get_client(
            p.base.user.login, p.base.repo.name, config.INSTALLATION_ID
        )
        pull = mergify_pull.MergifyPull(client, p.raw_data)

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
            ]
        )

    def test_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                        "approved-reviews-by=@mergifyio-testing/testing",
                    ],
                    "actions": {"merge": {"method": "rebase"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, commits = self.create_pr()

        client = github.get_client(
            p.base.user.login, p.base.repo.name, config.INSTALLATION_ID
        )
        pull = mergify_pull.MergifyPull(client, p.raw_data)

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
            ]
        )

    def _test_merge_custom_msg(self, header, method="squash"):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": method}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        msg = "This is the title\n\nAnd this is the message"
        p, _ = self.create_pr(message=f"It fixes it\n\n## {header}{msg}")
        self.create_status(p)

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)

        commit = self.r_o_admin.get_commits()[0].commit
        self.assertEqual(msg, commit.message)

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        checks = list(check_api.get_checks(pull))
        assert len(checks) == 2
        for check in checks:
            if check["name"] == "Summary":
                assert msg in check["output"]["summary"]
                break
        else:
            assert False, "Summary check not found"

    def test_merge_custom_msg(self):
        return self._test_merge_custom_msg("Commit Message:\n")

    def test_merge_custom_msg_case(self):
        return self._test_merge_custom_msg("Commit message\n")

    def test_merge_custom_msg_rn(self):
        return self._test_merge_custom_msg("Commit Message\r\n")

    def test_merge_custom_msg_merge(self):
        return self._test_merge_custom_msg("Commit Message:\n", "merge")

    def test_merge_and_closes_issues(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
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

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(2, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)
        self.assertEqual("closed", pulls[0].state)

        issues = list(self.r_o_admin.get_issues(state="all"))
        self.assertEqual(2, len(issues))
        self.assertEqual("closed", issues[0].state)
        self.assertEqual("closed", issues[1].state)

    def test_rebase(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
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

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)
        self.assertEqual("closed", pulls[0].state)

    def test_merge_branch_protection_ci(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": ["base=master"],
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

        self.branch_protection_protect("master", rule)

        p, _ = self.create_pr()

        self.wait_for(
            "check_run", {"check_run": {"conclusion": None, "status": "in_progress"}},
        )

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        checks = list(check_api.get_checks(pull, check_name="Rule: merge (merge)"))
        self.assertEqual(None, checks[0]["conclusion"])
        self.assertEqual("in_progress", checks[0]["status"])
        self.assertIn(
            "Waiting for the Branch Protection to be validated",
            checks[0]["output"]["title"],
        )

        self.create_status(p)

        self.wait_for("pull_request", {"action": "closed"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)

    def test_merge_branch_protection_strict(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "merge",
                    "conditions": [
                        "base=master",
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

        self.branch_protection_protect("master", rule)

        self.wait_for("pull_request", {"action": "closed"})

        self.create_status(p2)

        self.wait_for("check_run", {"check_run": {"conclusion": "failure"}})

        pull = mergify_pull.MergifyPull(self.cli_integration, p2.raw_data)
        checks = list(check_api.get_checks(pull, check_name="Rule: merge (merge)"))
        self.assertEqual("failure", checks[0]["conclusion"])
        self.assertIn(
            "Branch protection setting 'strict' conflicts with "
            "Mergify configuration",
            checks[0]["output"]["title"],
        )

    def _init_test_refresh(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": ["base!=master"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        rules = {
            "pull_request_rules": [
                {
                    "name": "automerge",
                    "conditions": ["label!=wip"],
                    "actions": {"merge": {}},
                }
            ]
        }

        self.git("checkout", "master")
        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(yaml.dump(rules))
        self.git("add", ".mergify.yml")
        self.git("commit", "--no-edit", "-m", "automerge everything")
        self.git("push", "--quiet", "main", "master")

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(2, len(pulls))
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

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_command_refresh(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": ["base!=master"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()

        pull = mergify_pull.MergifyPull(self.cli_integration, p.raw_data)
        check_api.set_check_run(
            pull,
            "Summary",
            "completed",
            "success",
            output={"title": "whatever", "summary": "erased"},
        )

        checks = list(check_api.get_checks(pull))
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        completed_at = checks[0]["completed_at"]

        p.create_issue_comment("@mergifyio refresh")

        self.wait_for("issue_comment", {"action": "created"})

        checks = list(check_api.get_checks(pull))
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"
        assert completed_at != checks[0]["completed_at"]

        p.update()
        comments = list(p.get_issue_comments())
        self.assertEqual("**Command `refresh`: success**", comments[-1].body)

    def test_refresh_branch(self):
        p1, p2 = self._init_test_refresh()

        self.app.post(
            "/refresh/%s/branch/master" % (p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_refresh_repo(self):
        p1, p2 = self._init_test_refresh()

        self.app.post(
            "/refresh/%s" % (p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC},
        )
        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_change_mergify_yml(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "nothing",
                    "conditions": ["base!=master"],
                    "actions": {"merge": {}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules))
        rules["pull_request_rules"].append(
            {"name": "foobar", "conditions": ["label!=wip"], "actions": {"merge": {}}}
        )
        p1, commits1 = self.create_pr(files={".mergify.yml": yaml.dump(rules)})
        pull = mergify_pull.MergifyPull(self.cli_integration, p1.raw_data)
        checks = list(check_api.get_checks(pull))
        assert len(checks) == 1
        assert checks[0]["name"] == "Summary"

    def test_marketplace_event(self):
        with mock.patch(
            "mergify_engine.branch_updater.sub_utils.get_subscription"
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
        assert r.data == b"Event queued"
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

        # Since we use celery eager system for testing, countdown= are ignored.
        # Wait a bit than Github refresh the mergeable_state before running the
        # engine
        if base.RECORD:
            time.sleep(10)

        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for(
            "issue_comment", {"action": "created", "comment": {"body": "It conflict!"}},
        )

    def test_command_update(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "nothing"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = self.create_pr(files={"TESTING2": "foobar"})
        p2, _ = self.create_pr(files={"TESTING3": "foobar"})
        p1.merge()

        self.wait_for("pull_request", {"action": "closed"})

        self.create_message(p2, "@mergifyio update")

        oldsha = p2.head.sha
        p2.update()
        assert p2.commits == 2
        assert oldsha != p2.head.sha

    def test_command_rebase_ok(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-rebase-on-conflict",
                    "conditions": ["conflict"],
                    "actions": {"comment": {"message": "@mergifyio rebase it please"}},
                }
            ]
        }
        self.setup_repo(yaml.dump(rules), files={"TESTING": "foobar"})
        p1, _ = self.create_pr(files={"TESTING": "foobar\np1"})
        p2, _ = self.create_pr(files={"TESTING": "p2\nfoobar"})
        p1.merge()

        # Since we use celery eager system for testing, countdown= are ignored.
        # Wait a bit than Github refresh the mergeable_state before running the
        # engine
        if base.RECORD:
            time.sleep(10)

        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("issue_comment", {"action": "created"})

        if base.RECORD:
            time.sleep(1)

        oldsha = p2.head.sha
        p2.update()
        assert oldsha != p2.head.sha
        assert p2.raw_data["mergeable_state"] != "conflict"

    def test_requested_reviews(self):
        team = list(self.o_admin.get_teams())[0]
        team.set_repo_permission(self.r_o_admin, "push")

        rules = {
            "pull_request_rules": [
                {
                    "name": "user",
                    "conditions": ["base=master", "review-requested=sileht"],
                    "actions": {"comment": {"message": "review-requested user"}},
                },
                {
                    "name": "team",
                    "conditions": ["base=master", "review-requested=@testing"],
                    "actions": {"comment": {"message": "review-requested team"}},
                },
            ],
        }
        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr()
        p1.create_review_request(reviewers=["sileht"])
        self.wait_for("pull_request", {"action": "review_requested"})
        self.wait_for("issue_comment", {"action": "created"})

        p2, _ = self.create_pr()
        p2.create_review_request(team_reviewers=[team.slug])
        self.wait_for("pull_request", {"action": "review_requested"})
        self.wait_for("issue_comment", {"action": "created"})

        self.assertEqual("review-requested user", list(p1.get_issue_comments())[0].body)
        self.assertEqual("review-requested team", list(p2.get_issue_comments())[0].body)

    def test_command_backport(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "auto-backport",
                    "conditions": ["base=master"],
                    "actions": {
                        "comment": {
                            "message": "@mergifyio backport stable/#3.1 feature/one"
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules), test_branches=["stable/#3.1", "feature/one"])
        p, _ = self.create_pr()

        self.wait_for("issue_comment", {"action": "created"})

        p.merge()
        self.wait_for("pull_request", {"action": "closed"})
        self.wait_for("issue_comment", {"action": "created"})

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(3, len(pulls))

    def test_truncated_check_output(self):
        # not used anyhow
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        self.setup_repo(yaml.dump(rules))
        pr, commits = self.create_pr()
        pull = mergify_pull.MergifyPull(self.cli_integration, pr.raw_data)
        check = check_api.set_check_run(
            pull,
            "Test",
            "completed",
            "success",
            {"summary": "a" * 70000, "title": "bla"},
        )
        assert check["output"]["summary"] == ("a" * 65532 + "…")

    def test_pull_request_complete(self):
        rules = {
            "pull_request_rules": [{"name": "noop", "conditions": [], "actions": {}}]
        }
        self.setup_repo(yaml.dump(rules))
        p, _ = self.create_pr()
        client = github.get_client(
            p.base.user.login, p.base.repo.name, config.INSTALLATION_ID
        )
        pull = mergify_pull.MergifyPull(client, {"number": 1})
        self.assertEqual(1, pull.data["number"])
        self.assertEqual("open", pull.data["state"])
        self.assertEqual("clean", pull.data["mergeable_state"])
