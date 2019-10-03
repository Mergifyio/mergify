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
import time
import unittest
from unittest import mock

import requests.exceptions

import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine.tasks.engine import actions_runner
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
        with open(actions_runner.mergify_rule_path, "r") as f:
            actions_runner.MERGIFY_RULE = yaml.safe_load(
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

        self.add_label_and_push_events(p, "backport-3.1")
        self.push_events(
            [("check_run", {"check_run": {"conclusion": None}})]  # Backport
        )
        p.remove_from_labels("backport-3.1")

        title = "The rule doesn't match anymore, " "this action has been cancelled"

        self.push_events(
            [
                ("pull_request", {"action": "unlabeled"}),
                (
                    "check_run",
                    {
                        "check_run": {
                            "conclusion": "neutral",
                            "output": {"title": title},
                        }
                    },
                ),
            ],
            ordered=False,
        )

        checks = list(
            check_api.get_checks(p, {"check_name": "Rule: backport (backport)"})
        )
        self.assertEqual("neutral", checks[0].conclusion)
        self.assertEqual(title, checks[0].output["title"])

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
        self.push_events(
            [
                ("check_suite", {"action": "requested"}),
                ("pull_request", {"action": "closed"}),
                ("push", {}),
            ],
            ordered=False,
        )

        p2, _ = self.create_pr(base_repo="main", branch="#2-second-pr")
        p2.edit(state="close")

        self.push_events([("pull_request", {"action": "closed"})], ordered=False)

        self.add_label_and_push_events(
            p1,
            "merge",
            [
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("push", {}),
                ("check_run", {"check_run": {"conclusion": "success"}}),  # Merge
            ],
        )
        self.add_label_and_push_events(
            p2,
            "close",
            [
                ("push", {}),
                ("check_run", {"check_run": {"conclusion": "success"}}),  # Merge
            ],
        )

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

        self.add_label_and_push_events(
            p1, "merge", [("check_run", {"check_run": {"conclusion": "success"}})]
        )

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

        self.add_label_and_push_events(
            p1,
            "merge",
            [
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("push", {}),
                ("pull_request", {"action": "closed"}),
            ],
        )

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

    @unittest.skip("Github API doesn't behave as the doc say")
    def test_request_reviews_teams(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "request_reviews",
                    "conditions": ["base=master"],
                    "actions": {
                        "request_reviews": {"teams": ["@mergifyio-testing/testing"]}
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        requests = pulls[0].get_review_requests()
        self.assertEqual(
            sorted(["@mergifyio-testing/testing"]),
            sorted([l.slug for l in requests[1]]),
        )

    def test_label(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "rename label",
                    "conditions": ["base=master", "label=stable"],
                    "actions": {
                        "label": {
                            "add": ["unstable", "foobar"],
                            "remove": ["stable", "what"],
                        }
                    },
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr()
        self.add_label_and_push_events(p, "stable")

        pulls = list(self.r_o_admin.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(
            sorted(["unstable", "foobar"]), sorted([l.name for l in pulls[0].labels])
        )

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
        self.add_label_and_push_events(
            p, "stable", [("issue_comment", {"action": "created"})]
        )

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

        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr()
        branch = "fork/pr%d" % self.pr_counter
        self.create_review_and_push_event(p, commits[-1], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        open(self.git.tmp + "/unwanted_changes", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes")
        self.git("commit", "--no-edit", "-m", "unwanted_changes")
        self.git("push", "--quiet", "fork", branch)

        self.push_events([("pull_request", {"action": "synchronize"})]),
        self.push_events(
            [
                ("check_suite", {"action": "completed"}),
                (
                    "check_run",
                    {
                        "check_run": {
                            "conclusion": "success",
                            "output": {"title": "1 rule matches"},
                        }
                    },
                ),
                (
                    "check_run",
                    {
                        "check_run": {
                            "conclusion": "success",
                            "output": {"title": "1 rule matches"},
                        }
                    },
                ),
                ("pull_request_review", {"action": "dismissed"}),
            ],
            ordered=False,
        )

        self.assertEqual(
            [("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        commits = list(p.get_commits())
        self.create_review_and_push_event(p, commits[-1], "REQUEST_CHANGES")

        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("CHANGES_REQUESTED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

        open(self.git.tmp + "/unwanted_changes2", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes2")
        self.git("commit", "--no-edit", "-m", "unwanted_changes2")
        self.git("push", "--quiet", "fork", branch)

        self.push_events([("pull_request", {"action": "synchronize"})]),
        self.push_events(
            [
                ("check_suite", {"action": "completed"}),
                (
                    "check_run",
                    {
                        "check_run": {
                            "conclusion": "success",
                            "output": {"title": "1 rule matches"},
                        }
                    },
                ),
                (
                    "check_run",
                    {
                        "check_run": {
                            "conclusion": "success",
                            "output": {"title": "1 rule matches"},
                        }
                    },
                ),
                ("pull_request_review", {"action": "dismissed"}),
            ],
            ordered=False,
        )

        self.assertEqual(
            [("DISMISSED", "mergify-test1"), ("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()],
        )

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

        self.add_label_and_push_events(p, "backport-#3.1")
        self.push_events([("pull_request", {"action": "closed"}), ("push", {})])

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].number)
        self.assertEqual(1, pulls[1].number)
        self.assertEqual(True, pulls[1].merged)
        self.assertEqual("closed", pulls[1].state)
        self.assertEqual(False, pulls[0].merged)

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ]
        )

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(
            [
                ("pull_request", {"action": "synchronize"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        self.assertEquals(1, len(commits2))
        self.assertNotEqual(commits[0].sha, commits2[0].sha)
        self.assertEqual(commits[0].commit.message, commits2[0].commit.message)

        # Retry to merge pr2
        self.create_status_and_push_event(p2)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ]
        )

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(
            [
                ("pull_request", {"action": "synchronize"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        # Retry to merge pr2
        self.create_status_and_push_event(p2)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ]
        )

        previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(
            [
                # Merge
                ("check_run", {"check_run": {"conclusion": None}})
            ]
        )

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

        self.push_events(
            [
                ("pull_request", {"action": "synchronize"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
                # Summary
                ("check_run", {"check_run": {"conclusion": "success"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        # Retry to merge pr2
        self.create_status_and_push_event(p2)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

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
        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ]
        )

        try:
            previous_master_sha = self.r_o_admin.get_commits()[0].sha
        except requests.exceptions.ConnectionError:
            # Please don't ask me why this call always fail..
            previous_master_sha = self.r_o_admin.get_commits()[0].sha

        self.create_status(p2, "continuous-integration/fake-ci", "success")
        self.push_events(
            [
                # fake-ci statuses
                ("status", {"state": "success"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
            ]
        )

        # We can run celery beat inside tests, so run the task manually
        run_smart_strict_workflow_periodic_task()

        self.push_events(
            [
                ("pull_request", {"action": "synchronize"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
                # Summary
                ("check_run", {"check_run": {"conclusion": "success"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        self.create_status(p3, "continuous-integration/fake-ci", "success")
        self.push_events(
            [
                ("status", {"state": "success"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
            ]
        )

        p2 = self.r_o_admin.get_pull(p2.number)
        commits2 = list(p2.get_commits())
        self.assertIn(
            "Merge branch 'master' into fork/pr2", commits2[-1].commit.message
        )

        self.create_status(p2, "continuous-integration/fake-ci", "failure")
        self.push_events([("status", {"state": "failure"})])
        self.push_events(
            [
                ("check_run", {"check_run": {"conclusion": "neutral"}}),
                ("check_suite", {"check_suite": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        # Should got to the next PR
        run_smart_strict_workflow_periodic_task()

        self.push_events(
            [
                ("pull_request", {"action": "synchronize"}),
                # Merge
                ("check_run", {"check_run": {"conclusion": None}}),
                # Summary
                ("check_run", {"check_run": {"conclusion": "success"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "success"}}),
            ],
            ordered=False,
        )

        p3 = self.r_o_admin.get_pull(p3.number)
        commits3 = list(p3.get_commits())
        self.assertIn("Merge branch 'master' into fork/pr", commits3[-1].commit.message)

        self.create_status(p3, "continuous-integration/fake-ci", "success")
        self.push_events(
            [
                ("status", {"state": "success"}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
                ("check_suite", {"action": "completed"}),
            ],
            ordered=False,
        )

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

        pull = mergify_pull.MergifyPull.from_raw(
            config.INSTALLATION_ID, config.MAIN_TOKEN, p.raw_data
        )

        logins = pull.resolve_teams(
            ["user", "@testing", "@unknown/team", "@invalid/team/break-here"]
        )

        assert sorted(logins) == sorted(
            [
                "user",
                "@unknown/team",
                "@invalid/team/break-here",
                "sileht",
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

        pull = mergify_pull.MergifyPull.from_raw(
            config.INSTALLATION_ID, config.MAIN_TOKEN, p.raw_data
        )

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
                "sileht",
                "mergify-test1",
            ]
        )

    def test_merge_custom_msg(self):
        rules = {
            "pull_request_rules": [
                {
                    "name": "Merge on master",
                    "conditions": [
                        "base=master",
                        "status-success=continuous-integration/fake-ci",
                    ],
                    "actions": {"merge": {"method": "squash"}},
                }
            ]
        }

        self.setup_repo(yaml.dump(rules))

        msg = "This is the title\n\nAnd this is the message"
        p, _ = self.create_pr(message="It fixes it\n\n## Commit Message:\n%s" % msg)
        self.create_status_and_push_event(p)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

        pulls = list(self.r_o_admin.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)

        commit = self.r_o_admin.get_commits()[0].commit
        self.assertEqual(msg, commit.message)

        checks = list(check_api.get_checks(p))
        assert len(checks) == 2
        assert checks[1].name == "Summary"
        assert msg in checks[1].output["summary"]

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
        self.create_status_and_push_event(p)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

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
        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

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

        self.push_events(
            [
                ("check_suite", {"check_suite": {"conclusion": "failure"}}),
                ("check_run", {"check_run": {"conclusion": "failure"}}),
            ]
        )

        checks = list(check_api.get_checks(p, {"check_name": "Rule: merge (merge)"}))
        self.assertEqual("failure", checks[0].conclusion)
        self.assertIn(
            "Branch protection settings are blocking " "automatic merging",
            checks[0].output["title"],
        )

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

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
            ],
            ordered=False,
        )

        self.create_status_and_push_event(p2)

        self.push_events(
            [
                # FIXME(sileht): Why twice ??
                # Merge
                ("check_run", {"check_run": {"conclusion": "failure"}}),
                # Merge
                ("check_run", {"check_run": {"conclusion": "failure"}}),
                ("check_suite", {"action": "completed"}),
            ],
            ordered=False,
        )

        checks = list(check_api.get_checks(p2, {"check_name": "Rule: merge (merge)"}))
        self.assertEqual("failure", checks[0].conclusion)
        self.assertIn(
            "Branch protection setting 'strict' conflicts with "
            "Mergify configuration",
            checks[0].output["title"],
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
        checks = list(check_api.get_checks(p1))
        assert len(checks) == 1
        assert checks[0].name == "Summary"

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
        time.sleep(10)

        self.push_events(
            [
                ("pull_request", {"action": "closed"}),
                ("push", {}),
                ("check_suite", {"action": "requested"}),
                (
                    "issue_comment",
                    {"action": "created", "comment": {"body": "It conflict!"}},
                ),
            ],
            ordered=False,
        )
