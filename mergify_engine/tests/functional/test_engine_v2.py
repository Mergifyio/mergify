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

import github

import yaml

from mergify_engine import branch_protection
from mergify_engine import check_api
from mergify_engine.tasks.engine import v2
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)

MERGE_EVENTS = [
    ("pull_request", {"action": "closed"}),
    ("check_run", {"check_run": {"conclusion": "success"}}),
    ("check_suite", {"action": "requested"}),
]


class TestEngineV2Scenario(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        with open(v2.mergify_rule_path, "r") as f:
            v2.MERGIFY_RULE = yaml.safe_load(f.read().replace(
                "mergify[bot]", "mergify-test[bot]"))
        super(TestEngineV2Scenario, self).setUp()

    def test_backport_cancelled(self):
        rules = {'pull_request_rules': [
            {"name": "backport",
             "conditions": [
                 "base=master",
                 "label=backport-3.1",
             ], "actions": {
                 "backport": {
                     "branches": ['stable/3.1'],
                 }}
             }
        ]}

        self.setup_repo(yaml.dump(rules), test_branches=['stable/3.1'])

        p, _ = self.create_pr(check="success")

        self.add_label_and_push_events(p, "backport-3.1")
        self.push_events([
            ("check_run", {"check_run": {"conclusion": None}}),
        ])
        p.remove_from_labels("backport-3.1")
        self.push_events([
            ("pull_request", {"action": "unlabeled"}),
            # Stupid bug, we must query the API instead ...
            # ("check_run", {"check_run": {"conclusion": "cancelled"}}),
        ], ordered=False)

        checks = list(check_api.get_checks(p, {
            "check_name": "Mergify — Rule: backport (backport)"}))
        self.assertEqual("cancelled", checks[0].conclusion)

    def test_delete_branch(self):
        rules = {'pull_request_rules': [
            {"name": "delete on merge",
             "conditions": [
                 "base=master",
                 "label=merge",
                 "merged",
             ], "actions": {
                 "delete_head_branch": {}}
             },
            {"name": "delete on close",
             "conditions": [
                 "base=master",
                 "label=close",
                 "closed",
             ], "actions": {
                 "delete_head_branch": {}}
             }
        ]}

        self.setup_repo(yaml.dump(rules))

        p1, _ = self.create_pr(check="success", base_repo="main")
        p1.merge()
        self.push_events([
            ("check_suite", {"action": "requested"}),
            ("pull_request", {"action": "closed"}),
        ], ordered=False)

        p2, _ = self.create_pr(check="success", base_repo="main")
        p2.edit(state="close")

        self.push_events([
            ("pull_request", {"action": "closed"}),
        ], ordered=False)

        self.add_label_and_push_events(p1, "merge")
        self.push_events([
            ("check_run", {"check_run": {"conclusion": "success"}}),
        ], ordered=False)
        self.add_label_and_push_events(p2, "close")
        self.push_events([
            ("check_run", {"check_run": {"conclusion": "success"}}),
        ], ordered=False)

        pulls = list(self.r_main.get_pulls(state="all"))
        self.assertEqual(2, len(pulls))

        for b in ("main/pr1", "main/pr2"):
            try:
                self.r_main.get_branch(b)
            except github.GithubException as e:
                if e.status == 404:
                    continue

            self.assertTrue(False, "branch %s not deleted" % b)

    def test_label(self):
        rules = {'pull_request_rules': [
            {"name": "rename label",
             "conditions": [
                 "base=master",
                 "label=stable",
             ], "actions": {
                 "label": {
                     "add": ['unstable', 'foobar'],
                     "remove": ['stable', 'what'],
                 }}
             }
        ]}

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr(check="success")
        self.add_label_and_push_events(p, "stable")

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual(sorted(["unstable", "foobar"]),
                         sorted([l.name for l in pulls[0].labels]))

    def test_close(self):
        rules = {'pull_request_rules': [
            {"name": "rename label",
             "conditions": [
                 "base=master",
             ], "actions": {
                 "close": {
                     "message": "WTF?"
                 }}
             }
        ]}

        self.setup_repo(yaml.dump(rules))

        p, _ = self.create_pr(check="success")

        p.update()
        self.assertEqual("closed", p.state)
        self.assertEqual("WTF?", list(p.get_issue_comments())[-1].body)

    def test_dismiss_reviews(self):
        rules = {'pull_request_rules': [
            {"name": "dismiss reviews",
             "conditions": [
                 "base=master",
             ], "actions": {
                 "dismiss_reviews": {
                     "approved": True,
                     "changes_requested": ["mergify-test1"],
                 }}
             }
        ]}

        self.setup_repo(yaml.dump(rules))
        p, commits = self.create_pr(check="success")
        branch = "fork/pr%d" % self.pr_counter
        self.create_review_and_push_event(p, commits[-1], "APPROVE")

        self.assertEqual(
            [("APPROVED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()]
        )

        open(self.git.tmp + "/unwanted_changes", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes")
        self.git("commit", "--no-edit", "-m", "unwanted_changes")
        self.git("push", "--quiet", "fork", branch)

        self.push_events([
            ("pull_request", {"action": "synchronize"}),
        ]),
        self.push_events([
            ("check_suite", {"action": "completed"}),
            ("check_run", {"check_run": {"conclusion": "success"}}),
            ("pull_request_review", {"action": "dismissed"}),
        ], ordered=False)

        self.assertEqual(
            [("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()]
        )

        commits = list(p.get_commits())
        self.create_review_and_push_event(p, commits[-1],
                                          "REQUEST_CHANGES")

        self.assertEqual(
            [("DISMISSED", "mergify-test1"),
             ("CHANGES_REQUESTED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()]
        )

        open(self.git.tmp + "/unwanted_changes2", "wb").close()
        self.git("add", self.git.tmp + "/unwanted_changes2")
        self.git("commit", "--no-edit", "-m", "unwanted_changes2")
        self.git("push", "--quiet", "fork", branch)

        self.push_events([
            ("pull_request", {"action": "synchronize"}),
        ]),
        self.push_events([
            ("check_suite", {"action": "completed"}),
            ("check_run", {"check_run": {"conclusion": "success"}}),
            ("pull_request_review", {"action": "dismissed"}),
        ], ordered=False)

        self.assertEqual(
            [("DISMISSED", "mergify-test1"),
             ("DISMISSED", "mergify-test1")],
            [(r.state, r.user.login) for r in p.get_reviews()]
        )

    def test_merge_backport(self):
        rules = {'pull_request_rules': [
            {"name": "Merge on master",
             "conditions": [
                 "base=master",
                 "status-success=continuous-integration/fake-ci",
                 "#approved-reviews-by>=1",
             ], "actions": {
                 "merge": {}
             }},
            {"name": "Backport to stable/3.1",
             "conditions": [
                 "base=master",
                 "label=backport-3.1",
             ], "actions": {
                 "backport": {
                     "branches": ['stable/3.1'],
                 }}
             },
            {"name": "automerge backport",
             "conditions": [
                 "head~=^mergify/bp/",
             ], "actions": {
                 "merge": {}
             }},
        ]}

        self.setup_repo(yaml.dump(rules), test_branches=['stable/3.1'])

        self.create_pr(check="success")
        p2, commits = self.create_pr(check="success")

        self.add_label_and_push_events(p2, "backport-3.1")
        self.push_events([
            ("check_run", {"check_run": {"conclusion": None}}),
        ])

        self.create_status_and_push_event(p2,
                                          context="not required status check",
                                          state="failure")
        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(MERGE_EVENTS, ordered=False)

        self.push_events([
            ("pull_request", {"action": "opened"}),
            ("check_suite", {"action": "requested"}),
            ("check_run", {"check_run": {"conclusion": "success"}}),
            ("check_suite", {"action": "completed"}),
        ], ordered=False)

        pulls = list(self.r_main.get_pulls(state="all"))
        self.assertEqual(3, len(pulls))
        self.assertEqual(3, pulls[0].number)
        self.assertEqual(2, pulls[1].number)
        self.assertEqual(1, pulls[2].number)
        self.assertEqual(True, pulls[1].merged)
        self.assertEqual("closed", pulls[1].state)

        self.assertEqual([], [b.name for b in self.r_main.get_branches()
                              if b.name.startswith("mergify/bp")])

    def test_merge_strict(self):
        rules = {'pull_request_rules': [
            {"name": "strict merge on master",
             "conditions": [
                 "base=master",
                 "status-success=continuous-integration/fake-ci",
                 "#approved-reviews-by>=1",
             ], "actions": {
                 "merge": {"strict": True}},
             }
        ]}

        self.setup_repo(yaml.dump(rules), test_branches=['stable/3.1'])

        p, _ = self.create_pr(check="success")
        p2, commits = self.create_pr(check="success")

        p.merge()
        self.push_events([
            ("pull_request", {"action": "closed"}),
            ("check_suite", {"action": "requested"}),
        ])

        previous_master_sha = self.r_main.get_commits()[0].sha

        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events([
            ("check_run", {"check_run": {"conclusion": None}}),
            ("pull_request", {"action": "synchronize"}),
            ("check_run", {"check_run": {"conclusion": "success"}}),
            ("check_suite", {"action": "completed"}),
        ], ordered=False)

        p2 = self.r_main.get_pull(p2.number)

        p2 = self.r_main.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn("Merge branch 'master' into 'fork/pr2'",
                      commits2[-1].commit.message)

        # Retry to merge pr2
        self.create_status_and_push_event(p2)

        self.push_events([
            ("pull_request", {"action": "closed"}),
            # We didn't receive this event... Github bug...
            # When a check_run is in_progress and move to completed
            # we never received the completed event
            # ("check_run", {"check_run": {"conclusion": "success"}}),
            ("check_suite", {"action": "requested"}),
        ], ordered=False)

        master_sha = self.r_main.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_rebase(self):
        rules = {'pull_request_rules': [
            {"name": "Merge on master",
             "conditions": [
                 "base=master",
                 "status-success=continuous-integration/fake-ci",
                 "#approved-reviews-by>=1",
             ], "actions": {
                 "merge": {"method": "rebase"}
             }},
        ]}

        self.setup_repo(yaml.dump(rules))

        p2, commits = self.create_pr(check="success")
        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        self.push_events(MERGE_EVENTS, ordered=False)

        pulls = list(self.r_main.get_pulls(state="all"))
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(True, pulls[0].merged)
        self.assertEqual("closed", pulls[0].state)

    def test_merge_branch_protection_ci(self):
        rules = {'pull_request_rules': [
            {"name": "merge",
             "conditions": [
                 "base=master",
             ], "actions": {
                 "merge": {}
             }},
        ]}

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

        branch_protection.protect(self.r_main, "master", rule)

        p, _ = self.create_pr(check="success")

        self.push_events([
            ("check_run", {"check_run": {"conclusion": "failure"}}),
        ])

        checks = list(check_api.get_checks(p, {
            "check_name": "Mergify — Rule: merge (merge)"}))
        self.assertEqual("failure", checks[0].conclusion)
        self.assertIn("Branch protection settings are blocking "
                      "automatic merging",
                      checks[0].output['title'])

    def test_merge_branch_protection_strict(self):
        rules = {'pull_request_rules': [
            {"name": "merge",
             "conditions": [
                 "base=master",
                 "status-success=continuous-integration/fake-ci",
             ], "actions": {
                 "merge": {}
             }},
        ]}

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

        branch_protection.protect(self.r_main, "master", rule)

        p1, _ = self.create_pr(check="success")
        p2, _ = self.create_pr(check="success")

        p1.merge()
        self.push_events([
            ("pull_request", {"action": "closed"}),
            ("check_suite", {"action": "requested"}),
        ])

        self.create_status_and_push_event(p2)
        self.push_events([
            ("check_run", {"check_run": {"conclusion": "failure"}}),
            ("check_suite", {"action": "completed"}),
        ], ordered=False)

        checks = list(check_api.get_checks(p2, {
            "check_name": "Mergify — Rule: merge (merge)"}))
        self.assertEqual("failure", checks[0].conclusion)
        self.assertIn("Branch protection setting 'strict' conflicts with "
                      "Mergify configuration",
                      checks[0].output['title'])
