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
# import copy
import json
import logging
import os
import re
import shutil
import time
import uuid
import yaml

import fixtures
import github
import requests
import requests.sessions
import rq
import testtools
import vcr

from mergify_engine import backports
from mergify_engine import config
from mergify_engine import engine
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import gh_update_branch
from mergify_engine import utils
from mergify_engine import web

gh_pr.monkeypatch_github()

LOG = logging.getLogger(__name__)

RECORD_MODE = os.getenv("MERGIFYENGINE_RECORD_MODE", "none")
CASSETTE_LIBRARY_DIR_BASE = 'mergify_engine/tests/fixtures/cassettes'
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))
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
    stable:
      protection:
        required_status_checks: null

"""


class GitterRecorder(utils.Gitter):
    def __init__(self, cassette_library_dir, suffix="main"):
        super(GitterRecorder, self).__init__()
        self.cassette_path = os.path.join(cassette_library_dir,
                                          "git-%s.json" % suffix)
        self.do_record = (
            RECORD_MODE == 'all' or
            (RECORD_MODE == 'once' and not os.path.exists(self.cassette_path))
        )

        if self.do_record:
            self.records = []
        else:
            self.load_records()

    def load_records(self):
        if not os.path.exists(self.cassette_path):
            raise RuntimeError("Cassette %s not found" % self.cassette_path)
        with open(self.cassette_path, 'rb') as f:
            data = f.read().decode('utf8')
            self.records = json.loads(data)

    def save_records(self):
        with open(self.cassette_path, 'wb') as f:
            data = json.dumps(self.records)
            f.write(data.encode('utf8'))

    def __call__(self, *args, **kwargs):
        if self.do_record:
            out = super(GitterRecorder, self).__call__(*args, **kwargs)
            self.records.append({"args": self.sanitize_uri(args),
                                 "kwargs": kwargs,
                                 "out": out.decode('utf8')})
            return out
        else:
            r = self.records.pop(0)
            assert r['args'] == self.sanitize_uri(args)
            assert r['kwargs'] == kwargs
            return r['out']

    @staticmethod
    def sanitize_uri(args):
        return [re.sub(r'://[^@]*@', "://<TOKEN>:@", arg)
                for arg in args]

    def cleanup(self):
        super(GitterRecorder, self).cleanup()
        if self.do_record:
            self.save_records()


class TestEngineScenario(testtools.TestCase):
    """Pastamaker engine tests

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        super(TestEngineScenario, self).setUp()

        self.cassette_library_dir = os.path.join(CASSETTE_LIBRARY_DIR_BASE,
                                                 self._testMethodName)

        if RECORD_MODE != "none":
            if os.path.exists(self.cassette_library_dir):
                shutil.rmtree(self.cassette_library_dir)
            os.makedirs(self.cassette_library_dir)

        self.recorder = vcr.VCR(
            cassette_library_dir=self.cassette_library_dir,
            record_mode=RECORD_MODE,
            match_on=['method', 'uri'],
            filter_headers=[
                ('Authorization', '<TOKEN>'),
                ('X-Hub-Signature', '<SIGNATURE>'),
                ('User-Agent', None),
                ('Accept-Encoding', None),
                ('Connection', None),
            ],
            before_record_response=self.remove_access_token,
            custom_patches=(
                (github.MainClass, 'HTTPSConnection',
                 vcr.stubs.VCRHTTPSConnection),
            )
        )

        self.useFixture(fixtures.MockPatchObject(
            gh_update_branch.utils, 'Gitter',
            lambda: GitterRecorder(self.cassette_library_dir)))

        self.useFixture(fixtures.MockPatchObject(
            backports.utils, 'Gitter',
            lambda: GitterRecorder(self.cassette_library_dir)))

        # Web authentification always pass
        self.useFixture(fixtures.MockPatch('hmac.compare_digest',
                                           return_value=True))

        reponame_path = os.path.join(self.cassette_library_dir, "reponame")

        gen_new_uuid = (
            RECORD_MODE == 'all' or
            (RECORD_MODE == 'once' and not os.path.exists(reponame_path))
        )

        if gen_new_uuid:
            REPO_UUID = str(uuid.uuid4())
            with open(reponame_path, "w") as f:
                f.write(REPO_UUID)
        else:
            with open(reponame_path, "r") as f:
                REPO_UUID = f.read()

        self.name = "repo-%s-%s" % (REPO_UUID, self._testMethodName)

        self.pr_counter = 0

        utils.setup_logging()
        config.log()

        self.git = GitterRecorder(self.cassette_library_dir, "tests")
        self.addCleanup(self.git.cleanup)

        web.app.testing = True
        self.app = web.app.test_client()

        # NOTE(sileht): Prepare a fresh redis
        self.redis = utils.get_redis_for_cache()
        self.redis.flushall()
        subscription = {"token": config.MAIN_TOKEN, "subscribed": False}
        self.redis.set("subscription-cache-%s" % config.INSTALLATION_ID,
                       json.dumps(subscription))

        # Let's start recording
        cassette = self.recorder.use_cassette("http.json")
        cassette.__enter__()
        self.addCleanup(cassette.__exit__)

        self.session = requests.Session()
        self.session.trust_env = False

        # Cleanup the remote testing redis
        r = self.session.delete(
            "https://gh.mergify.io/events-testing",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC})
        r.raise_for_status()

        self.g_main = github.Github(config.MAIN_TOKEN)
        self.g_fork = github.Github(config.FORK_TOKEN)

        self.u_main = self.g_main.get_user()
        self.u_fork = self.g_fork.get_user()
        assert self.u_main.login == "mergify-test1"
        assert self.u_fork.login == "mergify-test2"

        self.r_main = self.u_main.create_repo(self.name)
        self.url_main = "https://%s:@github.com/%s" % (
            config.MAIN_TOKEN, self.r_main.full_name)

        integration = github.GithubIntegration(config.INTEGRATION_ID,
                                               config.PRIVATE_KEY)

        access_token = integration.get_access_token(
            config.INSTALLATION_ID).token
        g = github.Github(access_token)
        user = g.get_user("mergify-test1")
        repo = user.get_repo(self.name)

        # Used to access the cache with its helper
        self.engine = engine.MergifyEngine(g, config.INSTALLATION_ID,
                                           access_token,
                                           subscription, user, repo)

        queue = rq.Queue(connection=utils.get_redis_for_rq())
        self.rq_worker = rq.SimpleWorker([queue],
                                         connection=queue.connection)

        if self._testMethodName != "test_creation_pull_of_initial_config":
            self.git("init")
            self.git("config", "user.name", "%s-tester" % config.CONTEXT)
            self.git("config", "user.email", "noreply@mergify.io")
            self.git("remote", "add", "main", self.url_main)
            with open(self.git.tmp + "/.mergify.yml", "w") as f:
                f.write(CONFIG)
            self.git("add", ".mergify.yml")
            self.git("commit", "--no-edit", "-m", "initial commit")
            self.git("push", "main", "master")

            self.git("checkout", "-b", "stable")
            self.git("push", "main", "stable")

            self.git("checkout", "-b", "nostrict")
            self.git("push", "main", "nostrict")

            self.r_fork = self.u_fork.create_fork(self.r_main)
            self.url_fork = "https://%s:@github.com/%s" % (
                config.FORK_TOKEN, self.r_fork.full_name)
            self.git("remote", "add", "fork", self.url_fork)
            self.git("fetch", "fork")

            # NOTE(sileht): Github looks buggy here:
            # We receive for the new repo the expected events:
            # * installation_repositories
            # * integration_installation_repositories
            # but we receive them 6 times with the same sha1...
            self.push_events(12)

    def tearDown(self):
        # NOTE(sileht): I'm guessing that deleting repository help to get
        # account flagged, so just keep them
        # self.r_fork.delete()
        # self.r_main.delete()
        super(TestEngineScenario, self).tearDown()

    @staticmethod
    def remove_access_token(response):
        try:
            decoded_response = json.loads(response["body"]["string"].decode())
        except ValueError:
            return response

        if "token" in decoded_response:
            decoded_response["token"] = "<TOKEN>"
            response["body"]["string"] = json.dumps(decoded_response).encode()
        return response

    def push_events(self, n=1):
        got = 0
        loop = 0
        while got < n:
            got += self._push_events()
            if got < n and RECORD_MODE in ["all", "once"]:
                time.sleep(0.1)
            loop += 1
            if loop > 100:
                raise RuntimeError("Never got expected events, "
                                   "%d instead of %d" % (got, n))
        if got != n:
            raise RuntimeError("We received more events than expected, "
                               "%d instead of %d" % (got, n))

    def _push_events(self):
        # NOTE(sileht): Simulate push Github events, we use a counter
        # for have each cassette call unique
        resp = self.session.get(
            "https://gh.mergify.io/events-testing",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        )
        events = resp.json()
        for event in reversed(events):
            self._send_event(**event)
        return len(events)

    def _send_event(self, id, type, payload):
        extra = payload.get("state", payload.get("action"))
        LOG.info("")
        LOG.info("=======================================================")
        LOG.info(">>> GOT EVENT: %s %s/%s" % (id, type, extra))
        r = self.app.post('/event', headers={
            "X-GitHub-Event": type,
            "X-GitHub-Delivery": "123456789",
            "X-Hub-Signature": "sha1=whatever",
            "Content-type": "application/json",
        }, data=json.dumps(payload))
        self.rq_worker.work(burst=True)
        return r

    def create_pr(self, base="master", files=None, two_commits=False):
        self.pr_counter += 1

        branch = "fork/pr%d" % self.pr_counter
        title = "Pull request n%d from fork" % self.pr_counter

        self.git("checkout", "fork/%s" % base, "-b", branch)
        if files:
            for name, content in files.items():
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                self.git("add", name)
        else:
            open(self.git.tmp + "/test%d" % self.pr_counter, "wb").close()
            self.git("add", "test%d" % self.pr_counter)
        self.git("commit", "--no-edit", "-m", title)
        if two_commits:
            self.git("mv", "test%d" % self.pr_counter,
                     "test%d-moved" % self.pr_counter)
            self.git("commit", "--no-edit", "-m", "%s, moved" % title)
        self.git("push", "fork", branch)

        p = self.r_fork.parent.create_pull(
            base=base,
            head="%s:%s" % (self.r_fork.owner.login, branch),
            title=title, body=title)

        # NOTE(sileht): This generated many events:
        # - pull_request : open
        # - status: Waiting for CI status (set by mergify)
        # - status: config checker if configuration change detected
        if files and ".mergify.yml" in files:
            self.push_events(3)
        else:
            self.push_events(2)

        # NOTE(sileht): We return the same but owned by the main project
        p = self.r_main.get_pull(p.number)
        commits = list(p.get_commits())

        return p, commits

    def create_status_and_push_event(self, pr, commit, excepted_events=1,
                                     context='continuous-integration/fake-ci',
                                     state='success'):
        # TODO(sileht): monkey patch PR with this
        _, data = self.r_main._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={'state': state,
                   'description': 'Your change works',
                   'context': context},
            headers={'Accept':
                     'application/vnd.github.machine-man-preview+json'}
        )
        self.push_events(excepted_events)

    def create_review_and_push_event(self, pr, commit, event="APPROVE"):
        r = pr.create_review(commit, "Perfect", event=event)
        self.push_events()
        return r

    def test_basic(self):
        self.create_pr()
        p2, commits = self.create_pr()

        # Check we have only on branch registered
        self.assertEqual("queues~%s~mergify-test1~%s~False~master"
                         % (config.INSTALLATION_ID, self.name),
                         self.engine.get_cache_key("master"))
        self.assertEqual(["master"], self.engine.get_cached_branches())

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

        self.assertTrue(gh_branch.is_configured(self.r_main, "master",
                                                expected_rule))

        # Checks the content of the cache
        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        for p in pulls:
            self.assertEqual(0, p.mergify_engine['status']['mergify_state'])

        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(2, len(r[0]['pulls']))
        self.assertEqual("master", r[0]['branch'])
        self.assertEqual(self.u_main.login, r[0]['owner'])
        self.assertEqual(self.r_main.name, r[0]['repo'])

        self.create_status_and_push_event(p2, commits[0],
                                          context="not required status check",
                                          state="error")
        self.create_status_and_push_event(p2, commits[0])
        self.create_review_and_push_event(p2, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].number)
        self.assertEqual(30,
                         pulls[0].mergify_engine['status']['mergify_state'])
        self.assertEqual("Will be merged soon",
                         pulls[0].mergify_engine['status'
                                                 ]['github_description'])

        self.assertEqual(1, pulls[1].number)
        self.assertEqual(0, pulls[1].mergify_engine['status']['mergify_state'])
        self.assertEqual("0/1 approvals required",
                         pulls[1].mergify_engine['status'
                                                 ]['github_description'])

        # Check the merged pull request is gone
        self.push_events(2)

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))

        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(1, len(r[0]['pulls']))

    def test_refresh_pull(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.build_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh/%s/pull/%s" % (
            p1.base.repo.full_name, p1.number),
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC})

        self.rq_worker.work(burst=True)

        self.app.post("/refresh/%s/pull/%s" % (
            p2.base.repo.full_name, p2.number),
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC})

        self.rq_worker.work(burst=True)

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(2, len(r[0]['pulls']))
        self.assertEqual("master", r[0]['branch'])
        self.assertEqual(self.u_main.login, r[0]['owner'])
        self.assertEqual(self.r_main.name, r[0]['repo'])

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.build_queue("master")
        self.assertEqual(0, len(pulls))

        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(0, len(r))

    def test_refresh_branch(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.build_queue("master")
        self.assertEqual(0, len(pulls))

        self.app.post("/refresh/%s/branch/master" % (
            p1.base.repo.full_name),
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC})

        self.rq_worker.work(burst=True)

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(2, len(r[0]['pulls']))
        self.assertEqual("master", r[0]['branch'])
        self.assertEqual(self.u_main.login, r[0]['owner'])
        self.assertEqual(self.r_main.name, r[0]['repo'])

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.build_queue("master")
        self.assertEqual(0, len(pulls))

        r = json.loads(self.app.get(
            '/status/install/%s/' % config.INSTALLATION_ID
        ).data.decode("utf8"))
        self.assertEqual(0, len(r))

    def test_disabling_files(self):
        p, commits = self.create_pr(files={"foobar": "what"})

        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(0, pulls[0].mergify_engine['status']['mergify_state'])
        self.assertEqual("Disabled, foobar is modified in this pull request",
                         pulls[0].mergify_engine['status'
                                                 ]['github_description'])

    def test_disabling_label(self):
        p, commits = self.create_pr()

        self.r_main.create_label("no-mergify", "000000")
        p.add_to_labels("no-mergify")

        self.push_events()

        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(0, pulls[0].mergify_engine['status']['mergify_state'])
        self.assertEqual("Disabled with label",
                         pulls[0].mergify_engine['status'
                                                 ]['github_description'])

    def test_auto_backport_failure(self):
        p, commits = self.create_pr("nostrict", two_commits=True)
        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])
        self.push_events(2)

        # Change the moved file of the previous PR
        p, commits = self.create_pr("nostrict", files={
            "test%d-moved" % self.pr_counter: "data"
        })

        # Backport it, but the file doesn't exists on the base branch
        self.r_main.create_label("bp-stable", "000000")
        p.add_to_labels("bp-stable")

        # Got label event
        self.push_events()

        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])

        # Got:
        # * status: mergify "Will be merged soon")
        # * pull_request : close event for pr
        self.push_events(3)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)
        self.assertIn("To fixup this pull request, you can check out it "
                      "locally", pulls[0].body)

        # Consume remaining events
        self.push_events(2)

    def test_auto_backport_rebase(self):
        p, commits = self.create_pr("nostrict", two_commits=True)

        self.r_main.create_label("bp-stable", "000000")
        p.add_to_labels("bp-stable")

        # Got label event
        self.push_events()

        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])

        # Got:
        # * status: mergify "Will be merged soon")
        # * pull_request : close event for pr
        self.push_events(3)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)

        # Consume remaining events
        self.push_events(2)

    def test_auto_backport_merge(self):
        p, commits = self.create_pr(two_commits=True)

        self.r_main.create_label("bp-stable", "000000")
        p.add_to_labels("bp-stable")

        # Got label event
        self.push_events()

        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0])

        # Got:
        # * status: mergify "Will be merged soon")
        # * pull_request : close event for pr
        self.push_events(4)

        pulls = list(self.r_main.get_pulls())
        self.assertEqual(1, len(pulls))
        self.assertEqual("stable", pulls[0].base.ref)
        self.assertEqual("Automatic backport of pull request #%d" % p.number,
                         pulls[0].title)

        # Consume remaining events
        self.push_events(1)

    def test_update_branch_strict(self):
        p1, commits1 = self.create_pr()
        p2, commits2 = self.create_pr()

        # merge the two PR
        self.create_status_and_push_event(p1, commits1[0])
        self.create_status_and_push_event(p2, commits2[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual("success", pulls[0].mergify_engine["combined_status"])
        self.assertEqual("success", pulls[1].mergify_engine["combined_status"])

        master_sha = self.r_main.get_commits()[0].sha

        self.create_review_and_push_event(p1, commits1[0])

        # Got:
        # * status: mergify "Will be merged soon")
        # * pull_request : close event for pr1
        self.push_events(2)

        # First PR merged
        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))

        previous_master_sha = master_sha
        master_sha = self.r_main.get_commits()[0].sha
        self.assertNotEqual(previous_master_sha, master_sha)

        # Try to merge pr2
        self.create_status_and_push_event(p2, commits2[0])
        self.create_review_and_push_event(p2, commits2[0])

        # Got
        # * pull_request : synchronise
        # * status: mergify "Wait for CI")
        self.push_events(2)

        p2 = self.r_main.get_pull(p2.number)
        commits2 = list(p2.get_commits())

        # Check master have been merged into the PR
        self.assertIn("Merge branch 'master' into 'fork/pr2'",
                      commits2[-1].commit.message)

        # Retry to merge pr2
        self.create_status_and_push_event(p2, commits2[0])

        # Got:
        # * status: mergify "Will be merged soon")
        # * pull_request : close event for pr2
        self.push_events(2)

        # Second PR merged
        pulls = self.engine.build_queue("master")
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
        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))

        self.assertEqual(1, pulls[0].mergify_engine["approvals"][2])

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

        self.assertTrue(gh_branch.is_configured(self.r_main, "master",
                                                expected_rule))

        p1 = self.r_main.get_pull(p1.number)
        commit = p1.base.repo.get_commit(p1.head.sha)
        ctxts = [s.raw_data["context"] for s in
                 reversed(list(commit.get_statuses()))]

        self.assertIn("mergify/future-config-checker", ctxts)

    def test_update_branch_disabled(self):
        p1, commits1 = self.create_pr("nostrict")
        p2, commits2 = self.create_pr("nostrict")

        # merge the two PR
        self.create_status_and_push_event(p1, commits1[0])
        self.create_status_and_push_event(p2, commits2[0])

        pulls = self.engine.build_queue("nostrict")
        self.assertEqual(2, len(pulls))
        self.assertEqual("success", pulls[0].mergify_engine["combined_status"])
        self.assertEqual("success", pulls[1].mergify_engine["combined_status"])

        self.create_review_and_push_event(p1, commits1[0])
        self.create_review_and_push_event(p2, commits2[0])

        # Got:
        # * status: mergify "Will be merged soon"
        # * pull_request : close event
        self.push_events(4)

        pulls = self.engine.build_queue("nostrict")
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
                    "required_approving_review_count": 2,
                },
                "restrictions": None,
                "enforce_admins": False,
            }
        }
        self.assertTrue(gh_branch.is_configured(self.r_main, "stable",
                                                expected_rule))

    def test_reviews(self):
        p, commits = self.create_pr()
        self.create_status_and_push_event(p, commits[0])
        self.create_review_and_push_event(p, commits[0],
                                          event="COMMENT")
        r = self.create_review_and_push_event(p, commits[0],
                                              event="REQUEST_CHANGES")

        self.push_events(2)

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual([], pulls[0].mergify_engine["approvals"][0])
        self.assertEqual("mergify-test1",
                         pulls[0].mergify_engine["approvals"][1][0]["login"])
        self.assertEqual(1, pulls[0].mergify_engine["approvals"][2])
        self.assertEqual(0, pulls[0].mergify_engine['status']['mergify_state'])
        self.assertEqual("pending",
                         pulls[0].mergify_engine['status']["github_state"])
        self.assertEqual("Change requests need to be dismissed",
                         pulls[0].mergify_engine['status'][
                             "github_description"])

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

        self.push_events(2)

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual([], pulls[0].mergify_engine["approvals"][0])
        self.assertEqual([], pulls[0].mergify_engine["approvals"][1])
        self.assertEqual(1, pulls[0].mergify_engine["approvals"][2])

        self.create_review_and_push_event(p, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual("mergify-test1",
                         pulls[0].mergify_engine["approvals"][0][0]["login"])
        self.assertEqual([], pulls[0].mergify_engine["approvals"][1])
        self.assertEqual(1, pulls[0].mergify_engine["approvals"][2])
        self.assertEqual(30,
                         pulls[0].mergify_engine['status']['mergify_state'])

    def test_creation_pull_of_initial_config(self):
        # FIXME(sileht): split setUp to not prepare useless resources

        self.git("init")
        self.git("config", "user.name", "%s-tester" % config.CONTEXT)
        self.git("config", "user.email", "noreply@mergify.io")
        self.git("remote", "add", "main", self.url_main)
        with open(self.git.tmp + "/randomfile", "w") as f:
            f.write("foobar")
        self.git("add", "randomfile")
        self.git("commit", "--no-edit", "-m", "initial commit")
        self.git("push", "main", "master")

        self.git("branch", "-M", "otherbranch")
        with open(self.git.tmp + "/secondfile", "w") as f:
            f.write("foobar")
        self.git("add", "secondfile")
        self.git("commit", "--no-edit", "-m", "second commit")
        self.git("push", "main", "otherbranch")

        p = self.r_main.create_pull(
            base="refs/heads/master",
            head="refs/heads/otherbranch",
            title="PR title", body="PR body")
        commits = list(p.get_commits())

        self.create_status_and_push_event(p, commits[0], excepted_events=0)

        self.push_events(16)

        pulls = list(self.r_main.get_pulls())

        self.assertEqual(2, len(pulls))

        files = list(pulls[0].get_files())

        self.assertEqual("Mergify initial configuration", pulls[0].title)
        self.assertEqual(1, len(files))
        self.assertEqual(".mergify.yml", files[0].filename)

        expected_config = """rules:
  protection:
    required_pull_request_reviews:
      required_approving_review_count: 1
    required_status_checks:
      contexts:
      - continuous-integration/fake-ci
"""
        got_config = self.session.get(files[0].raw_url).text

        self.assertEqual(expected_config, got_config)
