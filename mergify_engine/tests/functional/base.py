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
import copy
import json
import os
import re
import shutil
import subprocess
import time
import uuid

import daiquiri

import fixtures

import github

import requests
import requests.sessions

import testtools

import vcr

from mergify_engine import backports
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import utils
from mergify_engine import web
from mergify_engine import worker

LOG = daiquiri.getLogger(__name__)
RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = 'mergify_engine/tests/fixtures/cassettes'
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))


class GitterRecorder(utils.Gitter):
    def __init__(self, cassette_library_dir, suffix="main"):
        super(GitterRecorder, self).__init__()
        self.cassette_path = os.path.join(cassette_library_dir,
                                          "git-%s.json" % suffix)
        if RECORD:
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
        if RECORD:
            try:
                out = super(GitterRecorder, self).__call__(*args, **kwargs)
            except subprocess.CalledProcessError as e:
                self.records.append({"args": self.prepare_args(args),
                                     "kwargs": self.prepare_kwargs(kwargs),
                                     "exc": {
                                         "returncode": e.returncode,
                                         "cmd": e.cmd,
                                         "output": e.output.decode("utf8")}})
                raise
            else:
                self.records.append({"args": self.prepare_args(args),
                                     "kwargs": self.prepare_kwargs(kwargs),
                                     "out": out.decode('utf8')})
            return out
        else:
            r = self.records.pop(0)
            if "exc" in r:
                raise subprocess.CalledProcessError(
                    returncode=r['exc']['returncode'],
                    cmd=r['exc']['cmd'],
                    output=r['exc']['output'].encode('utf8')
                )
            else:
                assert r['args'] == self.prepare_args(args)
                assert r['kwargs'] == self.prepare_kwargs(kwargs)
                return r['out'].encode('utf8')

    def prepare_args(self, args):
        return [arg.replace(self.tmp, "/tmp/mergify-gitter<random>")
                for arg in args]

    @staticmethod
    def prepare_kwargs(kwargs):
        if "input" in kwargs:
            kwargs["input"] = re.sub(r'://[^@]*@', "://<TOKEN>:@",
                                     kwargs["input"].decode('utf8'))
        return kwargs

    def cleanup(self):
        super(GitterRecorder, self).cleanup()
        if RECORD:
            self.save_records()


# NOTE(sileht): Celery magic, this just skip amqp and execute tasks directly
# So all REST API calls will block and execute celery tasks directly
worker.app.conf.task_always_eager = True
worker.app.conf.task_eager_propagates = True


class FunctionalTestBase(testtools.TestCase):

    def setUp(self):
        super(FunctionalTestBase, self).setUp()
        self.pr_counter = 0
        self.cassette_library_dir = os.path.join(CASSETTE_LIBRARY_DIR_BASE,
                                                 self._testMethodName)

        # Recording stuffs
        if RECORD:
            if os.path.exists(self.cassette_library_dir):
                shutil.rmtree(self.cassette_library_dir)
            os.makedirs(self.cassette_library_dir)

        self.recorder = vcr.VCR(
            cassette_library_dir=self.cassette_library_dir,
            record_mode="all" if RECORD else "none",
            match_on=['method', 'uri'],
            filter_headers=[
                ('Authorization', '<TOKEN>'),
                ('X-Hub-Signature', '<SIGNATURE>'),
                ('User-Agent', None),
                ('Accept-Encoding', None),
                ('Connection', None),
            ],
            before_record_response=self.response_filter,
            custom_patches=(
                (github.MainClass, 'HTTPSConnection',
                 vcr.stubs.VCRHTTPSConnection),
            )
        )

        self.useFixture(fixtures.MockPatchObject(
            branch_updater.utils, 'Gitter',
            lambda: GitterRecorder(self.cassette_library_dir)))

        self.useFixture(fixtures.MockPatchObject(
            backports.utils, 'Gitter',
            lambda: GitterRecorder(self.cassette_library_dir)))

        # Web authentification always pass
        self.useFixture(fixtures.MockPatch('hmac.compare_digest',
                                           return_value=True))

        reponame_path = os.path.join(self.cassette_library_dir, "reponame")

        if RECORD:
            REPO_UUID = str(uuid.uuid4())
            with open(reponame_path, "w") as f:
                f.write(REPO_UUID)
        else:
            with open(reponame_path, "r") as f:
                REPO_UUID = f.read()

        self.name = "repo-%s-%s" % (REPO_UUID, self._testMethodName)

        utils.setup_logging()
        config.log()

        self.git = GitterRecorder(self.cassette_library_dir, "tests")
        self.addCleanup(self.git.cleanup)

        web.app.testing = True
        self.app = web.app.test_client()

        # NOTE(sileht): Prepare a fresh redis
        self.redis = utils.get_redis_for_cache()
        self.redis.flushall()
        self.subscription = {"token": config.MAIN_TOKEN, "subscribed": False}
        self.redis.set("subscription-cache-%s" % config.INSTALLATION_ID,
                       json.dumps(self.subscription))

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
        self.url_main = "https://github.com/%s" % self.r_main.full_name
        self.url_fork = "https://github.com/%s/%s" % (self.u_fork.login,
                                                      self.r_main.name)

        # Limit installations/subscription API to the test account
        install = {
            "id": config.INSTALLATION_ID,
            "target_type": "User",
            "account": {"login": "mergify-test1"}
        }

        self.useFixture(fixtures.MockPatch(
            'mergify_engine.utils.get_installations',
            lambda integration: [install]))

        real_get_subscription = utils.get_subscription

        def fake_subscription(r, install_id):
            if int(install_id) == config.INSTALLATION_ID:
                return real_get_subscription(r, install_id)
            else:
                return {"token": None, "subscribed": False}

        self.useFixture(fixtures.MockPatch(
            "mergify_engine.actions.merge.utils.get_subscription",
            side_effect=fake_subscription))

        self.useFixture(fixtures.MockPatch(
            "mergify_engine.web.utils.get_subscription",
            side_effect=fake_subscription))

        self.useFixture(fixtures.MockPatch(
            "github.MainClass.Installation.Installation.get_repos",
            return_value=[self.r_main]))

    def tearDown(self):
        # self.r_main.delete()
        super(FunctionalTestBase, self).tearDown()

        # NOTE(sileht): Wait a bit to ensure all remaining events arrive. And
        # also to avoid the "git clone fork" failure that Github returns when
        # we create repo too quickly
        if RECORD:
            time.sleep(0.5)

    def setup_repo(self, mergify_config, test_branches=[]):
        self.git("init")
        self.git.configure()
        self.git.add_cred(config.MAIN_TOKEN, "", self.r_main.full_name)
        self.git.add_cred(config.FORK_TOKEN, "", "%s/%s" %
                          (self.u_fork.login, self.r_main.name))
        self.git("config", "user.name", "%s-tester" % config.CONTEXT)
        self.git("remote", "add", "main", self.url_main)
        self.git("remote", "add", "fork", self.url_fork)

        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(mergify_config)
        self.git("add", ".mergify.yml")
        self.git("commit", "--no-edit", "-m", "initial commit")

        for test_branch in test_branches:
            self.git("branch", test_branch, "master")

        self.git("push", "--quiet", "main", "master", *test_branches)

        self.r_fork = self.u_fork.create_fork(self.r_main)
        self.git("fetch", "--quiet", "fork")

        # NOTE(sileht): Github looks buggy here:
        # We receive for the new repo the expected events:
        # * installation_repositories
        # * integration_installation_repositories
        self.drop_events(2)
        # NOTE(sileht): Since checks API have been enabled, we receive a
        # check request for the master branch head commit
        self.push_events([("check_suite", {"action": "requested"})])

    @staticmethod
    def response_filter(response):
        for h in ["X-GitHub-Request-Id", "Date", "ETag",
                  "X-RateLimit-Reset", "Expires", "Fastly-Request-ID",
                  "X-Timer", "X-Served-By", "Last-Modified",
                  "X-RateLimit-Remaining", "X-Runtime-rack"]:
            response["headers"].pop(h, None)
        return response

    def drop_events(self, total):
        events = []
        while len(events) < total:
            events.extend(list(self.session.get(
                "https://gh.mergify.io/events-testing?number=%d" % (
                    total - len(events)),
                data=FAKE_DATA,
                headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
            ).json()))

    @staticmethod
    def sorted_event(event):
        # Just allow randomly sorted dict
        return event[0], sorted(event[1].keys())

    def push_events(self, expected_events, ordered=True):
        LOG.debug("============= push events start =============")
        expected_events = copy.deepcopy(expected_events)
        total = len(expected_events)
        events = []
        loop = 0

        while len(events) < total:
            loop += 1
            if loop > 100:
                raise RuntimeError("Never got expected number of events, "
                                   "got %d events instead of %d" %
                                   (len(events), total))

            if RECORD:
                time.sleep(0.1)

            events += self._process_events(total - len(events))

        events = [(e["type"], e) for e in events]

        if not ordered:
            events = list(sorted(events, key=self.sorted_event))
            expected_events = list(sorted(expected_events,
                                          key=self.sorted_event))

        pos = 0
        for (etype, event), (expected_etype, expected_data) in \
                zip(events, expected_events):
            pos += 1

            if (expected_etype is not None and
                    expected_etype != etype):
                raise Exception(
                    "[%d] Got %s event type instead of %s" %
                    (pos, event["type"], expected_etype))

            self._validate_key(pos, event["payload"], expected_data)
        LOG.debug("============= push events end =============")

    @classmethod
    def _validate_key(cls, pos, data, expected_data):
        if isinstance(expected_data, dict):
            for key, expected in expected_data.items():
                if key in data:
                    cls._validate_key(pos, data[key], expected)
                else:
                    raise Exception("[%d] %s is missing" % (pos, key))
        else:
            if data != expected_data:
                raise Exception(
                    "[%d] Got %s instead of %s" %
                    (pos, data, expected_data))

    @classmethod
    def _remove_useless_links(cls, data):
        data.pop("installation", None)
        data.pop("sender", None)
        data.pop("repository", None)
        data.pop("base", None)
        data.pop("head", None)
        data.pop("id", None)
        data.pop("node_id", None)
        data.pop("tree_id", None)
        data.pop("_links", None)
        data.pop("user", None)
        data.pop("body", None)
        data.pop("after", None)
        data.pop("before", None)
        data.pop("app", None)
        data.pop("timestamp", None)
        data.pop("external_id", None)
        if "check_run" in data:
            data["check_run"].pop("checks_suite", None)
        for key, value in list(data.items()):
            if key.endswith("url"):
                del data[key]
            if key.endswith("_at"):
                del data[key]
            if isinstance(value, dict):
                data[key] = cls._remove_useless_links(value)
        return data

    @classmethod
    def _event_for_log(cls, event):
        filtered_payload = copy.deepcopy(event)
        return cls._remove_useless_links(filtered_payload)

    def _process_events(self, number):
        # NOTE(sileht): Simulate push Github events, we use a counter
        # for have each cassette call unique
        events = list(self.session.get(
            "https://gh.mergify.io/events-testing?number=%d" % number,
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        ).json())
        if events:
            for e in events:
                self._process_event(e["id"], e["type"], e["payload"])
        return events

    def _process_event(self, _id, _type, payload):  # noqa
        action = payload.get("action")
        if _type in ["check_run", "check_suite"]:
            extra = "/%s/%s" % (payload[_type].get("status"),
                                payload[_type].get("conclusion"))
        elif _type == "status":
            extra = "/%s" % payload.get("state")
        else:
            extra = ""
        LOG.debug("* Proceed event: [%s] %s/%s%s: %s",
                  _id, _type, action, extra,
                  self._event_for_log(payload))
        r = self.app.post('/event', headers={
            "X-GitHub-Event": _type,
            "X-GitHub-Delivery": "123456789",
            "X-Hub-Signature": "sha1=whatever",
            "Content-type": "application/json",
        }, data=json.dumps(payload))
        return r

    def create_pr(self, base="master", files=None, two_commits=False,
                  status="pending", check=None, base_repo="fork"):
        self.pr_counter += 1

        branch = "%s/pr%d" % (base_repo, self.pr_counter)
        title = "Pull request n%d from %s" % (self.pr_counter,
                                              base_repo)

        self.git("checkout", "--quiet", "%s/%s" % (base_repo, base),
                 "-b", branch)
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
        self.git("push", "--quiet", base_repo, branch)

        if base_repo == "fork":
            repo = self.r_fork.parent
            login = self.r_fork.owner.login
        else:
            repo = self.r_main
            login = self.r_main.owner.login

        p = repo.create_pull(
            base=base,
            head="%s:%s" % (login, branch),
            title=title, body=title)

        expected_events = [("pull_request", {"action": "opened"})]
        if files and ".mergify.yml" in files:
            # Yeah... we can receive opened after the check_run/suite ... it's
            # just random
            expected_events += [
                ("check_suite", {"check_suite": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "failure"}}),
            ]
        elif check:
            if base_repo == "main":
                expected_events += [
                    ("check_suite", {"check_suite": {"conclusion":
                                                     None}}),
                ]
            expected_events += [
                ("check_suite", {"check_suite": {"conclusion": "success"}}),
                ("check_run", {"check_run": {"conclusion": "success",
                                             "status": "completed"}}),
                ("check_run", {"check_run": {"conclusion": "success",
                                             "status": "completed"}})
            ]
        elif base != "disabled" and status:
            expected_events += [("status", {"state": status})]
        self.push_events(expected_events, ordered=False)

        # NOTE(sileht): We return the same but owned by the main project
        p = self.r_main.get_pull(p.number)
        commits = list(p.get_commits())

        return p, commits

    def create_status_and_push_event(self, pr,
                                     context='continuous-integration/fake-ci',
                                     state='success'):
        self.create_status(pr, context, state)
        self.push_events([("status", {"state": state})])

    def create_status(self, pr, context='continuous-integration/fake-ci',
                      state='success'):
        # TODO(sileht): monkey patch PR with this
        self.r_main._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={'state': state,
                   'description': 'Your change works',
                   'context': context},
            headers={'Accept':
                     'application/vnd.github.machine-man-preview+json'}
        )

    def create_review_and_push_event(self, pr, commit, event="APPROVE"):
        r = pr.create_review(commit, "Perfect", event=event)
        self.push_events([("pull_request_review", {"action": "submitted"})])
        return r

    def add_label_and_push_events(self, pr, label, additional_checks=[]):
        self.r_main.create_label(label, "000000")
        pr.add_to_labels(label)
        events = [("pull_request", {"action": "labeled"})]
        events.extend(additional_checks)
        self.push_events(events, ordered=False)

    def create_check_run_and_push_event(self, pr, name, conclusion=None,
                                        ignore_check_run_event=False,
                                        ignore_check_suite_event=False):
        if conclusion is None:
            status = "in_progress"
        else:
            status = "completed"

        pr_as_app = self.repo_as_app.get_pull(pr.number)
        check_api.set_check_run(pr_as_app, name, status, conclusion)

        expected_events = []
        if not ignore_check_run_event:
            expected_events.append(
                ("check_run", {"action": "created",
                               "check_run": {"conclusion": conclusion}}))
        if not ignore_check_suite_event:
            expected_events.append(
                ("check_suite", {'action': 'completed'}))

        self.push_events(expected_events, ordered=False)
