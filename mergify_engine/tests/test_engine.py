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
import itertools
import logging
import os
import re
import time
import uuid

import github
import testtools

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import utils

gh_pr.monkeypatch_github()

LOG = logging.getLogger(__name__)

MAIN_TOKEN = os.getenv("MERGIFYENGINE_MAIN_TOKEN")
FORK_TOKEN = os.getenv("MERGIFYENGINE_FORK_TOKEN")

CONFIG = """
policies:
  default:
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
  branches:
    master:
      required_pull_request_reviews:
        required_approving_review_count: 1
"""


@testtools.skipIf(FORK_TOKEN is None or
                  MAIN_TOKEN is None, "TOKEN missing")
class Tester(testtools.TestCase):
    """Pastamaker engine tests

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        super(Tester, self).setUp()
        self.name = str(uuid.uuid4())

        self.pr_counter = 0
        self.last_event_id = None

        utils.setup_logging()
        config.log()

        self.git = utils.Gitter()

        self.g_main = github.Github(MAIN_TOKEN)
        self.g_fork = github.Github(FORK_TOKEN)

        self.u_main = self.g_main.get_user()
        self.u_fork = self.g_fork.get_user()

        self.r_main = self.u_main.create_repo(self.name)
        self.url_main = "https://%s:@github.com/%s" % (
            MAIN_TOKEN, self.r_main.full_name)

        self.git("init")
        self.git("remote", "add", "main", self.url_main)

        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(CONFIG)
        self.git("add", ".mergify.yml")
        self.git("commit", "--no-edit", "-m", "initial commit")
        self.git("push", "main", "master")

        self.r_fork = self.u_fork.create_fork(self.r_main)
        self.url_fork = "https://%s:@github.com/%s" % (
            FORK_TOKEN, self.r_fork.full_name)
        self.git("remote", "add", "fork", self.url_fork)
        self.git("fetch", "fork")

        self.redis = utils.get_redis()
        self.redis.set("installation-token-0", MAIN_TOKEN)
        # FIXME(sileht): Use a GithubAPP token instead of
        # the main token, the API have tiny differences.
        # It's safe for basic testing, but in the future we should
        # use the correct token
        self.engine = engine.MergifyEngine(
            self.g_main, 0, self.u_main, self.r_main)
        self.push_events()

    def tearDown(self):
        super(Tester, self).tearDown()
        if self.git:
            self.git.cleanup()

        if os.getenv("MERGIFYENGINE_KEEP_REPO"):
            return
        if self.r_main:
            self.r_main.delete()
        if self.r_fork:
            self.r_fork.delete()

    def create_pr(self):
        branch = "fork/pr%d" % self.pr_counter
        title = "Pull request n%d from fork" % self.pr_counter

        self.git("checkout", "fork/master", "-b", branch)
        open(self.git.tmp + "/test%d" % self.pr_counter, "wb").close()
        self.git("add", "test%d" % self.pr_counter)
        self.git("commit", "--no-edit", "-m", title)
        self.git("push", "fork", branch)

        self.pr_counter += 1

        p = self.r_fork.parent.create_pull(
            base="master",
            head="%s:%s" % (self.r_fork.owner.login, branch),
            title=title, body=title)

        # NOTE(sileht): We return the same but owned by the main project
        return self.r_main.get_pull(p.number)

    def create_status_and_push_event(self, pr, commit):
        # NOTE(sileht): status events does not shown in
        # GET /repos/xxx/yyy/events API, so this method build a fake event and
        # push it directly to the engine

        # TODO(sileht): monkey patch PR with this
        _, data = self.r_main._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={'state': 'success',
                   'description': 'Your change works',
                   'context': 'continuous-integration/fake-ci'},
            headers={'Accept':
                     'application/vnd.github.machine-man-preview+json'}
        )
        LOG.info("Got event status")

        payload = {
            "id": data["id"],  # Not the event id, but we don't care
            "sha": pr.head.sha,
            "name": pr.base.repo.full_name,
            "target_url": data["target_url"],
            "context": data["context"],
            "description": data["description"],
            "state": data["state"],
            "commit": commit.raw_data,
            "branches": [],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "repository": pr.base.repo.raw_data,
            "sender": data["creator"],
            "organization": None,
            "installation": {"id": 0},
        }
        self.engine.handle("status", payload)

    def create_review_and_push_event(self, pr, commit):
        # NOTE(sileht): Same as status for pull_request_review
        r = pr.create_review(commit, "Perfect", event="APPROVE")
        payload = {
            "action": "submitted",
            "review": r.raw_data,
            "pull_request": pr.raw_data,
            "repository": pr.base.repo.raw_data,
            "organization": None,
            "installation": {"id": 0},

        }
        LOG.info("Got event pull_request_review")
        self.engine.handle("pull_request_review", payload)

    def push_events(self):
        # FIXME(sileht): maybe wait for a minimal number of event.
        time.sleep(0.1)

        # NOTE(sileht): Simulate push Github events
        events = list(
            itertools.takewhile(lambda e: e.id != self.last_event_id,
                                self.r_main.get_events()))
        if events:
            self.last_event_id = events[0].id

        for event in reversed(events):
            # NOTE(sileht): PullRequestEvent -> pull_request
            etype = re.sub('([A-Z]{1})', r'_\1', event.type)[1:-6].lower()
            LOG.info("Got event %s" % etype)
            self.engine.handle(etype, event.payload)

    def test_basic(self):
        self.create_pr()
        p2 = self.create_pr()
        self.push_events()

        # Check we have only on branch registered
        self.assertEqual(["master"], self.engine.get_cached_branches())

        # Check policy of that branch is the expected one
        expected_policy = {
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
        self.assertTrue(gh_branch.is_protected(self.r_main, "master",
                                               expected_policy))

        # Checks the content of the cache
        pulls = self.engine.load_cache("master")
        self.assertEqual(2, len(pulls))
        for p in pulls:
            self.assertEqual(-1, p["mergify_engine_weight"])

        commits = list(p2.get_commits())

        # Post CI status
        self.create_status_and_push_event(p2, commits[0])
        # Approve the patch
        self.create_review_and_push_event(p2, commits[0])

        pulls = self.engine.load_cache("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0]['number'])
        self.assertEqual(11, pulls[0]['mergify_engine_weight'])

        self.assertEqual(1, pulls[1]['number'])
        self.assertEqual(-1, pulls[1]['mergify_engine_weight'])

        # Check the merged pull request is gone
        self.push_events()
        pulls = self.engine.load_cache("master")
        self.assertEqual(1, len(pulls))

    def test_refresh(self):
        self.create_pr()
        self.create_pr()
        self.push_events()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.load_cache("master")
        self.assertEqual(0, len(pulls))

        # Test pr refresh API
        self.engine.handle("refresh", {
            'repository': self.r_main.raw_data,
            'installation': {'id': '0'},
            "refresh_ref": "pull/2",
        })
        pulls = self.engine.load_cache("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(2, pulls[0]['number'])
        self.assertEqual(-1, pulls[0]['mergify_engine_weight'])

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.load_cache("master")
        self.assertEqual(0, len(pulls))

        # Test full refresh API
        self.engine.handle("refresh", {
            'repository': self.r_main.raw_data,
            'installation': {'id': '0'},
            "refresh_ref": "branch/master",
        })
        pulls = self.engine.load_cache("master")
        self.assertEqual(2, len(pulls))
        # TODO(sileht): Do it per installation basis
