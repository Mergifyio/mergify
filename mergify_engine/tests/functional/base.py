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
import os
import re
import shutil
import subprocess
import uuid

import fixtures

import github

import requests
import requests.sessions

import testtools

import vcr

from mergify_engine import backports
from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import utils
from mergify_engine import web
from mergify_engine import worker


RECORD_MODE = os.getenv("MERGIFYENGINE_RECORD_MODE", "none")
CASSETTE_LIBRARY_DIR_BASE = 'mergify_engine/tests/fixtures/cassettes'
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))


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
        if self.do_record:
            self.save_records()


# NOTE(sileht): Celery magic, this just skip amqp and execute tasks directly
# So all REST API calls will block and execute celery tasks directly
worker.app.conf.task_always_eager = True
worker.app.conf.task_eager_propagates = True


class FunctionalTestBase(testtools.TestCase):

    def setUp(self):
        super(FunctionalTestBase, self).setUp()

        self.cassette_library_dir = os.path.join(CASSETTE_LIBRARY_DIR_BASE,
                                                 self._testMethodName)

        # Recording stuffs
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
            if install_id == config.INSTALLATION_ID:
                return real_get_subscription(r, install_id)
            else:
                return {"token": None, "subscribed": False}

        self.useFixture(fixtures.MockPatch(
            "mergify_engine.web.utils.get_subscription",
            side_effect=fake_subscription))

        self.useFixture(fixtures.MockPatch(
            "github.MainClass.Installation.Installation.get_repos",
            return_value=[self.r_main]))

    def tearDown(self):
        # self.r_main.delete()
        super(FunctionalTestBase, self).tearDown()

    @staticmethod
    def response_filter(response):
        for h in ["X-GitHub-Request-Id", "Date", "ETag",
                  "X-RateLimit-Reset", "Expires", "Fastly-Request-ID",
                  "X-Timer", "X-Served-By", "Last-Modified",
                  "X-RateLimit-Remaining", "X-Runtime-rack"]:
            response["headers"].pop(h, None)
        return response
