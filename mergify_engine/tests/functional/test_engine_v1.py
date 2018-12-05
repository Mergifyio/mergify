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
from mergify_engine import config
from mergify_engine import rules
from mergify_engine.tasks.engine import v1
from mergify_engine.tests.functional import base


LOG = logging.getLogger(__name__)

CONFIG = """
rules:
  default:
    protection:
      required_status_checks:
        strict: True
        contexts:
            - continuous-integration/fake-ci
      required_pull_request_reviews:
        dismiss_stale_reviews: true
        require_code_owner_reviews: false
        required_approving_review_count: 2
      restrictions: null
      enforce_admins: false
    disabling_files:
      - foo?ar
    automated_backport_labels:
      bp-stable: stable
      bp-not-exist: not-exist

  branches:
    master:
      protection:
        required_pull_request_reviews:
          required_approving_review_count: 1
    nostrict:
      protection:
        required_status_checks:
          strict: False
        required_pull_request_reviews:
          required_approving_review_count: 1
      merge_strategy:
        method: rebase
    enabling_label:
      protection:
        required_status_checks:
          strict: False
        required_pull_request_reviews:
          required_approving_review_count: 1
      merge_strategy:
        method: rebase
      enabling_label: go-mergify
    stable:
      protection:
        required_pull_request_reviews:
          required_approving_review_count: 1
        required_status_checks: null
    disabled: null

"""

MERGE_EVENTS = [
    ("status", {"state": "success"}),  # Will be merged soon
    ("status", {"state": "success"}),  # Merged
    ("pull_request", {"action": "closed"}),
    ("check_suite", {"action": "requested"}),
]


class TestEngineScenario(base.FunctionalTestBase):
    """Mergify engine tests.

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        super(TestEngineScenario, self).setUp()
        # Used to access the cache with its helper
        self.processor = v1.Processor(self.subscription,
                                      self.r_main,
                                      config.INSTALLATION_ID,
                                      self.installation_token)

        self.setup_repo(CONFIG, ['stable', 'nostrict', 'disabled',
                                 'enabling_label'])

    def _get_queue(self, branch):
        config = rules.get_mergify_config(self.r_main)
        branch_rule = rules.get_branch_rule(config['rules'], branch)
        collaborators = [self.u_main.id]
        return self.processor._build_queue(branch, branch_rule, collaborators)

    def test_branch_disabled(self):
        old_rule = {
            "protection": {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["continuous-integration/no-ci"],
                },
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": 1,
                },
                "restrictions": None,
                "enforce_admins": False,
            }
        }
        branch_protection.protect(self.r_main, "disabled", old_rule)

        config = rules.get_mergify_config(self.r_main)
        rule = rules.get_branch_rule(config['rules'], "disabled")
        self.assertEqual(None, rule)
        data = branch_protection.get_protection(self.r_main, "disabled")
        self.assertFalse(branch_protection.is_configured(
            self.r_main, "disabled", rule, data))

        self.create_pr("disabled")
        self.assertEqual([], self.processor._get_cached_branches())
        self.assertEqual([], self._get_queue("disabled"))

        data = branch_protection.get_protection(self.r_main, "disabled")
        self.assertTrue(branch_protection.is_configured(
            self.r_main, "disabled", rule, data))

    def test_basic(self):
        self.create_pr()
        p2, commits = self.create_pr()

        # Check we have only on branch registered
        self.assertEqual("queues~%s~mergifyio-testing~%s~False~master"
                         % (config.INSTALLATION_ID, self.name),
                         self.processor._get_cache_key("master"))
        self.assertEqual(["master"], self.processor._get_cached_branches())

        # Check policy of that branch is the expected one
        expected_rule = {
            "protection": {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["continuous-integration/fake-ci"],
                },
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": 1,
                },
                "restrictions": None,
                "enforce_admins": False,
            }
        }

        data = branch_protection.get_protection(self.r_main, "master")
        self.assertTrue(branch_protection.is_configured(
            self.r_main, "master", expected_rule, data))

        # Checks the content of the cache
        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))
        for p in pulls:
            self.assertEqual(0, p.mergify_state)

        self.create_status_and_push_event(p2,
                                          context="not required status check",
                                          state="error")
        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits[0])

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].g_pull.number)
        self.assertEqual(30,
                         pulls[0].mergify_state)
        self.assertEqual("Will be merged soon",
                         pulls[0].github_description)

        self.assertEqual(1, pulls[1].g_pull.number)
        self.assertEqual(0, pulls[1].mergify_state)
        self.assertEqual("0/1 approvals required",
                         pulls[1].github_description)

        # Check the merged pull request is gone
        self.push_events(MERGE_EVENTS)

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))

    def test_refresh_pull(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh/%s/pull/%s" % (
            p1.base.repo.full_name, p1.number),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC})

        self.app.post("/refresh/%s/pull/%s" % (
            p2.base.repo.full_name, p2.number),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC})

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

    def test_refresh_branch(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh/%s/branch/master" % (
            p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC})

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

    def test_refresh_repo(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh/%s/full" % (
            p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC})

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

    def test_refresh_all(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh",
                      headers={"X-Hub-Signature": "sha1=" + base.FAKE_HMAC})

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))

        # Erase the cache and check the engine is empty
        self.redis.delete(self.processor._get_cache_key("master"))
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

    def test_disabling_files(self):
        p, commits = self.create_pr(files={"foobar": "what"}, status="failure")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].g_pull.number)
        self.assertEqual(0, pulls[0].mergify_state)
        self.assertEqual("Disabled — foobar is modified",
                         pulls[0].github_description)

    def test_enabling_label(self):
        p, commits = self.create_pr("enabling_label", status="failure")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        pulls = self._get_queue("enabling_label")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].g_pull.number)
        self.assertEqual(0, pulls[0].mergify_state)
        self.assertEqual("Disabled — enabling label missing",
                         pulls[0].github_description)

    def test_disabling_label(self):
        p, commits = self.create_pr()

        self.add_label_and_push_events(p, "no-mergify")
        self.push_events([("status", {"state": "failure"})])

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].g_pull.number)
        self.assertEqual(0, pulls[0].mergify_state)
        self.assertEqual("Disabled — disabling label present",
                         pulls[0].github_description)

    def test_auto_backport_branch_not_exists(self):
        p, commits = self.create_pr("nostrict", two_commits=True)
        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])
        self.push_events(MERGE_EVENTS)

        # Change the moved file of the previous PR
        p, commits = self.create_pr("nostrict", files={
            "test%d-moved" % self.pr_counter: "data"
        })

        # Backport it, but the file doesn't exists on the base branch
        self.add_label_and_push_events(p, "bp-not-exist")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        self.push_events(MERGE_EVENTS)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_auto_backport_failure(self):
        p, commits = self.create_pr("nostrict", two_commits=True)
        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])
        self.push_events(MERGE_EVENTS)

        # Change the moved file of the previous PR
        p, commits = self.create_pr("nostrict", files={
            "test%d-moved" % self.pr_counter: "data"
        })

        # Backport it, but the file doesn't exists on the base branch
        self.add_label_and_push_events(p, "bp-stable")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        self.push_events(MERGE_EVENTS)
        self.push_events([
            ("check_suite", {"action": "requested"}),  # for backport branch
            ("pull_request", {"action": "opened"}),
            ("status", {"state": "pending"}),
        ])

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)
        self.assertIn("Cherry-pick of", pulls[0].body)
        self.assertIn("have failed", pulls[0].body)
        self.assertIn("To fixup this pull request, you can check out it "
                      "locally", pulls[0].body)

    def test_auto_backport_rebase(self):
        p, commits = self.create_pr("nostrict", two_commits=True)

        self.add_label_and_push_events(p, "bp-stable")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        self.push_events(MERGE_EVENTS)
        self.push_events([
            ("check_suite", {"action": "requested"}),  # for backport branch
            ("pull_request", {"action": "opened"}),
        ], ordered=False)
        self.push_events([
            ("status", {"state": "pending"}),
        ])

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)

    def test_auto_backport_not_merged_pr(self):
        p, commits = self.create_pr("nostrict", two_commits=True)

        self.r_main.create_label("bp-stable", "000000")
        p.add_to_labels("bp-stable")
        p.edit(state="closed")
        self.push_events([
            ("pull_request", {"action": "labeled"}),
            ("pull_request", {"action": "closed"}),
        ])

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

    def test_auto_backport_closed_pr(self):
        p, commits = self.create_pr(two_commits=True)

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        self.push_events(MERGE_EVENTS)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

        self.r_main.create_label("bp-stable", "000000")
        p.add_to_labels("bp-stable")
        self.push_events([
            ("pull_request", {"action": "labeled"}),
        ])
        self.push_events([
            ("check_suite", {"action": "requested"}),  # for backport branch
            ("pull_request", {"action": "opened"}),
        ], ordered=False)
        self.push_events([
            ("status", {"state": "pending"}),
        ])
        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)

        # Ensure temporary bp branch exists
        bp_branch_ref = "heads/%s" % pulls[0].head.ref
        self.r_main.get_git_ref(bp_branch_ref)

        commits = list(pulls[0].get_commits())
        self.create_review_and_push_event(pulls[0], commits[0])

        self.push_events(MERGE_EVENTS)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

        self.assertRaises(github.UnknownObjectException,
                          self.r_main.get_git_ref,
                          bp_branch_ref)

    def test_auto_backport_merge(self):
        p, commits = self.create_pr(two_commits=True)

        self.add_label_and_push_events(p, "bp-stable")

        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0])

        self.push_events(MERGE_EVENTS)
        self.push_events([
            ("check_suite", {"action": "requested"}),  # for backport branch
            ("pull_request", {"action": "opened"}),
        ], ordered=False)
        self.push_events([
            ("status", {"state": "pending"}),
        ])

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)

        # Ensure temporary bp branch exists
        bp_branch_ref = "heads/%s" % pulls[0].head.ref
        self.r_main.get_git_ref(bp_branch_ref)

        commits = list(pulls[0].get_commits())
        self.create_review_and_push_event(pulls[0], commits[0])

        self.push_events(MERGE_EVENTS)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(0, len(pulls))

        self.assertRaises(github.UnknownObjectException,
                          self.r_main.get_git_ref,
                          bp_branch_ref)

    def test_update_branch_strict(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # merge the two PR
        self.create_status_and_push_event(p1)
        self.create_status_and_push_event(p2)

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertTrue(pulls[0]._required_statuses)
        self.assertTrue(pulls[1]._required_statuses)

        master_sha = self.r_main.get_commits()[0].sha

        self.create_review_and_push_event(p1, commits1[0])

        self.push_events(MERGE_EVENTS)

        # First PR merged
        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))

        previous_master_sha = master_sha
        master_sha = self.r_main.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        # Try to merge pr2
        self.create_status_and_push_event(p2)
        self.create_review_and_push_event(p2, commits2[0])

        self.push_events([
            ("status", {"state": "pending"}),
            ("pull_request", {"action": "synchronize"}),
            ("status", {"state": "pending"}),
        ])

        p2 = self.r_main.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn("Merge branch 'master' into 'fork/pr2'",
                      commits2[-1].commit.message)

        # Retry to merge pr2
        self.create_status_and_push_event(p2)

        self.push_events(MERGE_EVENTS)

        # Second PR merged
        pulls = self._get_queue("master")
        self.assertEqual(0, len(pulls))

        previous_master_sha = master_sha
        master_sha = self.r_main.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

    def test_change_mergify_yml(self):
        config = yaml.load(CONFIG)
        config["rules"]["branches"]["master"]["protection"][
            "required_pull_request_reviews"][
                "required_approving_review_count"] = 6
        config = yaml.dump(config)
        p1, commits1 = self.create_pr(files={".mergify.yml": config})
        checks = list(check_api.get_checks(p1))
        assert len(checks) == 2
        assert checks[0].name == ("Mergify — disabled due to configuration "
                                  "change")
        assert checks[1].name == "Mergify — future config checker"

    def test_update_branch_disabled(self):
        p1, commits1 = self.create_pr("nostrict")
        p2, commits2 = self.create_pr("nostrict")

        # merge the two PR
        self.create_status_and_push_event(p1)
        self.create_status_and_push_event(p2)

        pulls = self._get_queue("nostrict")
        self.assertEqual(2, len(pulls))
        self.assertTrue(pulls[0]._required_statuses)
        self.assertTrue(pulls[1]._required_statuses)

        self.create_review_and_push_event(p1, commits1[0])
        self.push_events(MERGE_EVENTS)

        self.create_review_and_push_event(p2, commits2[0])
        self.push_events(MERGE_EVENTS)
        pulls = self._get_queue("nostrict")
        self.assertEqual(0, len(pulls))

        p2 = self.r_main.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have not been merged into the PR
        self.assertNotIn("Merge branch", commits2[-1].commit.message)

    def test_missing_required_status_check(self):
        self.create_pr("stable")

        # Check policy of that branch is the expected one
        expected_rule = {
            "protection": {
                "required_status_checks": None,
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": 1,
                },
                "restrictions": None,
                "enforce_admins": False,
            }
        }
        data = branch_protection.get_protection(self.r_main, "stable")
        self.assertTrue(branch_protection.is_configured(self.r_main, "stable",
                                                        expected_rule, data))

    def test_reviews(self):
        p, commits = self.create_pr()
        self.create_status_and_push_event(p)
        self.create_review_and_push_event(p, commits[0], event="COMMENT")
        r = self.create_review_and_push_event(p, commits[0],
                                              event="REQUEST_CHANGES")
        self.push_events([("status", {"state": "pending"})])

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual([], pulls[0]._reviews_ok)
        self.assertEqual("mergify-test1", pulls[0]._reviews_ko[0]["login"])
        self.assertEqual(1, pulls[0]._reviews_required)
        self.assertEqual(0, pulls[0].mergify_state)
        self.assertEqual("pending", pulls[0].github_state)
        self.assertEqual("Change requests need to be dismissed",
                         pulls[0].github_description)

        self.r_main._requester.requestJsonAndCheck(
            'PUT',
            "{base_url}/pulls/{number}/reviews/{review_id}/dismissals".
            format(
                base_url=self.r_main.url,
                number=p.number, review_id=r.id
            ),
            input={"message": "message"},
            headers={'Accept':
                     'application/vnd.github.luke-cage-preview+json'}
        )

        self.push_events([
            ("pull_request_review", {"action": "dismissed"}),
            ("status", {"state": "pending"})
        ])

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual([], pulls[0]._reviews_ok)
        self.assertEqual([], pulls[0]._reviews_ko)
        self.assertEqual(1, pulls[0]._reviews_required)

        self.create_review_and_push_event(p, commits[0])

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual("mergify-test1", pulls[0]._reviews_ok[0]["login"])
        self.assertEqual([], pulls[0]._reviews_ko)
        self.assertEqual(1, pulls[0]._reviews_required)
        self.assertEqual(30, pulls[0].mergify_state)

    def test_checks(self):
        self.create_pr()
        p2, commits = self.create_pr()

        # Check we have only on branch registered
        self.assertEqual("queues~%s~mergifyio-testing~%s~False~master"
                         % (config.INSTALLATION_ID, self.name),
                         self.processor._get_cache_key("master"))
        self.assertEqual(["master"], self.processor._get_cached_branches())

        # Check policy of that branch is the expected one
        expected_rule = {
            "protection": {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["continuous-integration/fake-ci"],
                },
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": 1,
                },
                "restrictions": None,
                "enforce_admins": False,
            }
        }

        data = branch_protection.get_protection(self.r_main, "master")
        self.assertTrue(branch_protection.is_configured(
            self.r_main, "master", expected_rule, data))

        # Checks the content of the cache
        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))
        for p in pulls:
            self.assertEqual(0, p.mergify_state)

        self.create_check_run_and_push_event(p2, "The always broken check",
                                             conclusion="failure",
                                             check_suite=True)

        self.create_check_run_and_push_event(p2,
                                             'continuous-integration/fake-ci',
                                             conclusion=None)

        self.create_check_run_and_push_event(p2,
                                             'continuous-integration/fake-ci',
                                             conclusion="success",
                                             created=False)

        self.create_check_run_and_push_event(p2, 'Another check',
                                             conclusion="success")

        self.create_review_and_push_event(p2, commits[0])

        pulls = self._get_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].g_pull.number)
        self.assertEqual(30, pulls[0].mergify_state)
        self.assertEqual("Will be merged soon",
                         pulls[0].github_description)

        self.assertEqual(1, pulls[1].g_pull.number)
        self.assertEqual(0, pulls[1].mergify_state)
        self.assertEqual("0/1 approvals required",
                         pulls[1].github_description)

        # Check the merged pull request is gone
        self.push_events(MERGE_EVENTS)

        pulls = self._get_queue("master")
        self.assertEqual(1, len(pulls))
