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
import json
import logging
import os
import uuid

import betamax
from betamax_serializers.pretty_json import PrettyJSONSerializer
import fixtures
import github
import requests
import rq
import testtools

from mergify_engine import config
from mergify_engine import engine
from mergify_engine import gh_branch
from mergify_engine import gh_pr
from mergify_engine import gh_update_branch
from mergify_engine import utils
from mergify_engine import web

gh_pr.monkeypatch_github()

LOG = logging.getLogger(__name__)

MAIN_TOKEN = os.getenv("MERGIFYENGINE_MAIN_TOKEN", "<MAIN_TOKEN>")
FORK_TOKEN = os.getenv("MERGIFYENGINE_FORK_TOKEN", "<FORK_TOKEN>")
INSTALLATION_ID = os.getenv("MERGIFYENGINE_INSTALLATION_ID")
RECORD_MODE = 'none' if MAIN_TOKEN == "<MAIN_TOKEN>" else 'all'

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
  branches:
    master:
      protection:
        required_pull_request_reviews:
          required_approving_review_count: 1
    stable:
      protection:
        required_status_checks: null

"""


betamax.Betamax.register_serializer(PrettyJSONSerializer)

with betamax.Betamax.configure() as c:
    c.cassette_library_dir = 'mergify_engine/tests/fixtures/http_cassettes'
    c.default_cassette_options.update({
        'record_mode': RECORD_MODE,
        'match_requests_on': ['method', 'uri', 'headers'],
        'serialize_with': 'prettyjson',
    })
    c.define_cassette_placeholder("<MAIN_TOKEN>", MAIN_TOKEN)
    c.define_cassette_placeholder("<FORK_TOKEN>", FORK_TOKEN)
    c.define_cassette_placeholder("<PRIVATE_KEY>", config.PRIVATE_KEY)

    if not os.path.exists(c.cassette_library_dir):
        os.makedirs(c.cassette_library_dir)


class GitterRecorder(utils.Gitter):
    cassette_library_dir = 'mergify_engine/tests/fixtures/git_cassettes'

    def __init__(self, cassette_name):
        super(GitterRecorder, self).__init__()
        self.name = cassette_name
        self.cassette_path = os.path.join(self.cassette_library_dir,
                                          "%s.json" % self.name)
        if RECORD_MODE == 'all':
            self.records = []
        else:
            self.load_records()

    def load_records(self):
        if not os.path.exists(self.cassette_path):
            raise RuntimeError("Cassette %s not found" % self.cassette_path)
        with open(self.cassette_path, 'rb') as f:
            self.records = json.loads(f.read().decode('utf8'))

    def save_records(self):
        if not os.path.exists(self.cassette_library_dir):
            os.makedirs(self.cassette_library_dir)
        with open(self.cassette_path, 'wb') as f:
            data = json.dumps(self.records, sort_keys=True,
                              indent=4, separators=(',', ': '))
            f.write(data.replace(
                MAIN_TOKEN, "<MAIN_TOKEN>").replace(
                    FORK_TOKEN, "<FORK_TOKEN>").encode('utf8'))

    def __call__(self, *args, **kwargs):
        if RECORD_MODE == 'all':
            out = super(GitterRecorder, self).__call__(*args, **kwargs)
            self.records.append({"args": args, "kwargs": kwargs, "out":
                                 out.decode('utf8')})
            return out
        else:
            r = self.records.pop(0)
            assert r['args'] == list(args)
            assert r['kwargs'] == kwargs
            return r['out']

    def cleanup(self):
        super(GitterRecorder, self).cleanup()
        if RECORD_MODE == 'all':
            self.save_records()


class TestEngineScenario(testtools.TestCase):
    """Pastamaker engine tests

    Tests user github resource and are slow, so we must reduce the number
    of scenario as much as possible for now.
    """

    def setUp(self):
        super(TestEngineScenario, self).setUp()
        session = requests.Session()
        session.trust_env = False
        self.recorder = betamax.Betamax(session)
        self.cassette = lambda n, *args, **kwargs: self.recorder.use_cassette(
            self._testMethodName + "_" + n, *args, **kwargs)

        self.useFixture(fixtures.MockPatchObject(
            requests, 'Session', return_value=session))

        self.useFixture(fixtures.MockPatchObject(
            gh_update_branch.utils, 'Gitter',
            lambda: GitterRecorder(self._testMethodName)))

        # Web authentification always pass
        self.useFixture(fixtures.MockPatch('hmac.compare_digest',
                                           return_value=True))

        if RECORD_MODE == "none":
            with open("mergify_engine/tests/fixtures/repo_uuid", "r") as f:
                record_uuid = f.read()
        else:
            record_uuid = str(uuid.uuid4())
            with open("mergify_engine/tests/fixtures/repo_uuid", "w") as f:
                f.write(record_uuid)

        self.name = "repo-%s-%s" % (record_uuid, self._testMethodName)

        self.event_call_counter = 0
        self.events_counter = 0
        self.pr_counter = 0
        self.last_event_id = None

        utils.setup_logging()
        config.log()

        self.git = GitterRecorder('%s-tests' % self._testMethodName)
        self.addCleanup(self.git.cleanup)

        web.app.testing = True
        self.app = web.app.test_client()

        # NOTE(sileht): Prepare a fresh redis
        self.redis = utils.get_redis()
        self.redis.flushall()
        self.redis.set("installation-token-%s" % INSTALLATION_ID, MAIN_TOKEN)

        with self.cassette("setUp"):
            # Cleanup the remote testing redis
            r = requests.delete("https://gh.mergify.io/events-testing")
            r.raise_for_status()

            self.g_main = github.Github(MAIN_TOKEN)
            self.g_fork = github.Github(FORK_TOKEN)

            self.u_main = self.g_main.get_user()
            self.u_fork = self.g_fork.get_user()
            assert self.u_main.login == "mergify-test1"
            assert self.u_fork.login == "mergify-test2"

            self.r_main = self.u_main.create_repo(self.name)
            self.url_main = "https://%s:@github.com/%s" % (
                MAIN_TOKEN, self.r_main.full_name)
            self.git("init")
            self.git("config", "user.name", "%s-bot" % config.CONTEXT)
            self.git("config", "user.email", "noreply@mergify.io")
            self.git("remote", "add", "main", self.url_main)
            with open(self.git.tmp + "/.mergify.yml", "w") as f:
                f.write(CONFIG)
            self.git("add", ".mergify.yml")
            self.git("commit", "--no-edit", "-m", "initial commit")
            self.git("push", "main", "master")

            self.git("checkout", "-b", "stable")
            self.git("push", "main", "stable")

            self.r_fork = self.u_fork.create_fork(self.r_main)
            self.url_fork = "https://%s:@github.com/%s" % (
                FORK_TOKEN, self.r_fork.full_name)
            self.git("remote", "add", "fork", self.url_fork)
            self.git("fetch", "fork")

            queue = rq.Queue(connection=self.redis)
            self.rq_worker = rq.SimpleWorker([queue],
                                             connection=queue.connection)

            # NOTE(sileht): Github looks buggy here:
            # We receive for the new repo the expected events:
            # * installation_repositories
            # * integration_installation_repositories
            # but we receive them 6 times with the same sha1...
            self.push_events(12)

            # Create a engine, to use helper its methods to control
            # the redis content
            integration = github.GithubIntegration(config.INTEGRATION_ID,
                                                   config.PRIVATE_KEY)
            token = integration.get_access_token(INSTALLATION_ID).token
            g = github.Github(token)
            user = g.get_user(self.u_main.login)
            repo = user.get_repo(self.name)
            self.engine = engine.MergifyEngine(g, INSTALLATION_ID, user, repo)

    def tearDown(self):
        super(TestEngineScenario, self).tearDown()

        # NOTE(sileht): I'm guessing that deleting repository help to get
        # account flagged, so just keep them
        # with self.cassette("tearDown"):
        #     self.r_fork.delete()
        #     self.r_main.delete()

    def _push_events(self):
        # NOTE(sileht): Simulate push Github events, we use a counter
        # for have each cassette call unique
        self.event_call_counter += 1
        with self.cassette("events-%s-getter" % self.event_call_counter):
            resp = requests.get("https://gh.mergify.io/events-testing?id=%s" %
                                self.event_call_counter)
        events = resp.json()
        for event in events:
            self.event_call_counter += 1
            with self.cassette("events-%s-handler" % self.event_call_counter):
                LOG.info("GOT EVENT: %s/%s" % (event["type"], event["id"]))
                self._send_event(event["type"], event["payload"])
        return len(events)

    def push_events(self, n=1):
        got = 0
        while got < n:
            got += self._push_events()
        if got != n:
            raise RuntimeError("We received more events than expected")

    def _send_event(self, etype, data):
        r = self.app.post('/event', headers={
            "X-GitHub-Event": etype,
            "X-GitHub-Delivery": "123456789",
            "X-Hub-Signature": "sha1=whatever",
            "Content-type": "application/json",
        }, data=json.dumps(data))
        self.rq_worker.work(burst=True)
        return r

    def create_pr(self, base="master"):
        branch = "fork/pr%d" % self.pr_counter
        title = "Pull request n%d from fork" % self.pr_counter

        self.git("checkout", "fork/%s" % base, "-b", branch)
        open(self.git.tmp + "/test%d" % self.pr_counter, "wb").close()
        self.git("add", "test%d" % self.pr_counter)
        self.git("commit", "--no-edit", "-m", title)
        self.git("push", "fork", branch)

        self.pr_counter += 1

        p = self.r_fork.parent.create_pull(
            base=base,
            head="%s:%s" % (self.r_fork.owner.login, branch),
            title=title, body=title)

        # NOTE(sileht): This generated many events:
        # - pull_request : open
        # - status: Waiting for CI status (set by mergify)
        self.push_events(2)

        # NOTE(sileht): We return the same but owned by the main project
        return self.r_main.get_pull(p.number)

    def create_status_and_push_event(self, pr, commit):
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
        self.push_events()

    def create_review_and_push_event(self, pr, commit):
        r = pr.create_review(commit, "Perfect", event="APPROVE")
        self.push_events()
        return r

    def test_basic(self):
        with self.cassette("create_pr1", allow_playback_repeats=True):
            self.create_pr()
        with self.cassette("create_pr2", allow_playback_repeats=True):
            p2 = self.create_pr()
            commits = list(p2.get_commits())

        # Check we have only on branch registered
        self.assertEqual("queues~%s~%s~%s~master"
                         % (INSTALLATION_ID, self.u_main.login, self.name),
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
        with self.cassette("branch"):
            self.assertTrue(gh_branch.is_configured(self.r_main, "master",
                                                    expected_rule))

        # Checks the content of the cache
        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        for p in pulls:
            self.assertEqual(-1, p.mergify_engine['weight'])

        r = json.loads(self.app.get('/status/install/' + INSTALLATION_ID
                                    ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(2, len(r[0]['pulls']))
        self.assertEqual("master", r[0]['branch'])
        self.assertEqual(self.u_main.login, r[0]['owner'])
        self.assertEqual(self.r_main.name, r[0]['repo'])

        with self.cassette("status"):
            # Post CI status
            self.create_status_and_push_event(p2, commits[0])
        with self.cassette("review"):
            # Approve the patch
            self.create_review_and_push_event(p2, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        self.assertEqual(2, pulls[0].number)
        self.assertEqual(11, pulls[0].mergify_engine['weight'])
        self.assertEqual("Will be merged soon",
                         pulls[0].mergify_engine['status_desc'])

        self.assertEqual(1, pulls[1].number)
        self.assertEqual(-1, pulls[1].mergify_engine['weight'])
        self.assertEqual("Waiting for approvals",
                         pulls[1].mergify_engine['status_desc'])

        # Check the merged pull request is gone
        self.push_events(2)

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))

        r = json.loads(self.app.get('/status/install/' + INSTALLATION_ID
                                    ).data.decode("utf8"))
        self.assertEqual(1, len(r))
        self.assertEqual(1, len(r[0]['pulls']))

    def test_refresh(self):
        with self.cassette("create_pr1", allow_playback_repeats=True):
            p1 = self.create_pr()
        with self.cassette("create_pr2", allow_playback_repeats=True):
            p2 = self.create_pr()

        # Erase the cache and check the engine is empty
        self.redis.delete(self.engine.get_cache_key("master"))
        pulls = self.engine.build_queue("master")
        self.assertEqual(0, len(pulls))

        with self.cassette("refresh-1"):
            self.engine.handle("refresh", {
                'repository': self.r_main.raw_data,
                'installation': {'id': '0'},
                "pull_request": p1.raw_data,
            })
        with self.cassette("refresh-2"):
            self.engine.handle("refresh", {
                'repository': self.r_main.raw_data,
                'installation': {'id': INSTALLATION_ID},
                "pull_request": p2.raw_data,
            })

        pulls = self.engine.build_queue("master")
        self.assertEqual(2, len(pulls))
        r = json.loads(self.app.get('/status/install/' + INSTALLATION_ID
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

        r = json.loads(self.app.get('/status/install/' + INSTALLATION_ID
                                    ).data.decode("utf8"))
        self.assertEqual(0, len(r))

    def test_disabling_label(self):
        with self.cassette("create_pr", allow_playback_repeats=True):
            p = self.create_pr()
            commits = list(p.get_commits())
            self.r_main.create_label("no-mergify", "000000")
            p.add_to_labels("no-mergify")
            self.push_events()

        with self.cassette("status"):
            # Post CI status
            self.create_status_and_push_event(p, commits[0])
        with self.cassette("review"):
            # Approve the patch
            self.create_review_and_push_event(p, commits[0])

        pulls = self.engine.build_queue("master")
        self.assertEqual(1, len(pulls))
        self.assertEqual(1, pulls[0].number)
        self.assertEqual(-1, pulls[0].mergify_engine['weight'])
        self.assertEqual("Disabled by label",
                         pulls[0].mergify_engine['status_desc'])

    def test_missing_required_status_check(self):
        with self.cassette("create_pr", allow_playback_repeats=True):
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
        with self.cassette("branch"):
            self.assertTrue(gh_branch.is_configured(self.r_main, "stable",
                                                    expected_rule))
