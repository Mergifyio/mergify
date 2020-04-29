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
import asyncio
import copy
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

import github as pygithub
import httpx
import pytest
import redis
from starlette import testclient
import vcr

from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine import logs
from mergify_engine import sub_utils
from mergify_engine import tasks
from mergify_engine import utils
from mergify_engine import web
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.clients import github_app


LOG = logs.getLogger(__name__)
RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = "zfixtures/cassettes"
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))

# NOTE(sileht): Celery magic, this just skip amqp and execute tasks directly
# So all REST API calls will block and execute celery tasks directly
tasks.app.conf.task_always_eager = True
tasks.app.conf.task_eager_propagates = True


class GitterRecorder(utils.Gitter):
    def __init__(self, logger, cassette_library_dir, suffix):
        super(GitterRecorder, self).__init__(logger)
        self.cassette_path = os.path.join(cassette_library_dir, "git-%s.json" % suffix)
        if RECORD:
            self.records = []
        else:
            self.load_records()

    def load_records(self):
        if not os.path.exists(self.cassette_path):
            raise RuntimeError("Cassette %s not found" % self.cassette_path)
        with open(self.cassette_path, "rb") as f:
            data = f.read().decode("utf8")
            self.records = json.loads(data)

    def save_records(self):
        with open(self.cassette_path, "wb") as f:
            data = json.dumps(self.records)
            f.write(data.encode("utf8"))

    def __call__(self, *args, **kwargs):
        if RECORD:
            try:
                out = super(GitterRecorder, self).__call__(*args, **kwargs)
            except subprocess.CalledProcessError as e:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "exc": {
                            "returncode": e.returncode,
                            "cmd": e.cmd,
                            "output": e.output.decode("utf8"),
                        },
                    }
                )
                raise
            else:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "out": out.decode("utf8"),
                    }
                )
            return out
        else:
            r = self.records.pop(0)
            if "exc" in r:
                raise subprocess.CalledProcessError(
                    returncode=r["exc"]["returncode"],
                    cmd=r["exc"]["cmd"],
                    output=r["exc"]["output"].encode("utf8"),
                )
            else:
                assert r["args"] == self.prepare_args(args), "%s != %s" % (
                    r["args"],
                    self.prepare_args(args),
                )
                assert r["kwargs"] == self.prepare_kwargs(kwargs), "%s != %s" % (
                    r["kwargs"],
                    self.prepare_kwargs(kwargs),
                )
                return r["out"].encode("utf8")

    def prepare_args(self, args):
        return [arg.replace(self.tmp, "/tmp/mergify-gitter<random>") for arg in args]

    @staticmethod
    def prepare_kwargs(kwargs):
        if "input" in kwargs:
            kwargs["input"] = re.sub(
                r"://[^@]*@", "://<TOKEN>:@", kwargs["input"].decode("utf8")
            )
        return kwargs

    def cleanup(self):
        super(GitterRecorder, self).cleanup()
        if RECORD:
            self.save_records()


class WorkerThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self._running = threading.Event()

    async def async_run(self):
        w = worker.Worker(idle_sleep_time=0.42 if RECORD else 0.01)
        w.start()
        while self._running.is_set():
            await asyncio.sleep(0.42 if RECORD else 0.02)
        w.stop()
        await w.wait_shutdown_complete()

    def run(self):
        self._running.set()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.async_run())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    def stop(self):
        if self._running.is_set():
            self._running.clear()
            self.join()


class EventReader:
    def __init__(self, app):
        self._app = app
        self._session = httpx.Client(trust_env=False)
        self._handled_events = queue.Queue()
        self._counter = 0
        self._redis = redis.StrictRedis.from_url(
            config.STREAM_URL, decode_responses=True
        )

    def close(self):
        self._redis.close()

    def drain(self):
        # NOTE(sileht): Drop any pending events still on the server
        r = self._session.request(
            "DELETE",
            "https://gh.mergify.io/events-testing",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        )
        r.raise_for_status()
        self._redis.flushall()

    def wait_for(self, event_type, expected_payload, timeout=60 if RECORD else 2):
        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout:
            try:
                event = self._handled_events.get(block=False)
            except queue.Empty:
                for event in self._get_events():
                    self._forward_to_engine_api(event)
                    self._handled_events.put(event)
                    self._wait_all_streams_proceed(started_at, timeout)
                else:
                    if RECORD:
                        time.sleep(1)
                continue

            if event["type"] == event_type and self._match(
                event["payload"], expected_payload
            ):
                return

        raise Exception(
            f"Never got event `{event_type}` with payload `{expected_payload}` (timeout)"
        )

    @classmethod
    def _match(cls, data, expected_data):
        if isinstance(expected_data, dict):
            for key, expected in expected_data.items():
                if key not in data:
                    return False
                if not cls._match(data[key], expected):
                    return False
            return True
        else:
            return data == expected_data

    def _get_events(self):
        # NOTE(sileht): we use a counter to make each call unique in cassettes
        self._counter += 1
        return self._session.request(
            "GET",
            f"https://gh.mergify.io/events-testing?counter={self._counter}",
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        ).json()

    def _wait_all_streams_proceed(self, started_at, timeout):
        streams = self._redis.zrange("streams", start=0, end=-1)
        while time.monotonic() - started_at < timeout and streams:
            time.sleep(1 if RECORD else 0.01)
            streams = self._redis.zrange("streams", start=0, end=-1)
            LOG.debug("Remaining streams: %s", streams)

    def _forward_to_engine_api(self, event):
        payload = event["payload"]
        if event["type"] in ["check_run", "check_suite"]:
            extra = "/%s/%s" % (
                payload[event["type"]].get("status"),
                payload[event["type"]].get("conclusion"),
            )
        elif event["type"] == "status":
            extra = "/%s" % payload.get("state")
        else:
            extra = ""
        LOG.warning(
            "### EVENT RECEIVED ### [%s] %s/%s%s: %s",
            event["id"],
            event["type"],
            payload.get("action"),
            extra,
            self._remove_useless_links(copy.deepcopy(event)),
        )
        r = self._app.post(
            "/event",
            headers={
                "X-GitHub-Event": event["type"],
                "X-GitHub-Delivery": "123456789",
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
            data=json.dumps(payload),
        )
        return r

    @classmethod
    def _remove_useless_links(cls, data):
        if isinstance(data, dict):
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
            if "organization" in data:
                data["organization"].pop("description", None)
            if "check_run" in data:
                data["check_run"].pop("checks_suite", None)
            for key, value in list(data.items()):
                if key.endswith("url"):
                    del data[key]
                elif key.endswith("_at"):
                    del data[key]
                else:
                    data[key] = cls._remove_useless_links(value)
            return data
        elif isinstance(data, list):
            return [cls._remove_useless_links(elem) for elem in data]
        else:
            return data


@pytest.mark.usefixtures("logger_checker")
class FunctionalTestBase(unittest.TestCase):
    def setUp(self):
        super(FunctionalTestBase, self).setUp()
        self.pr_counter = 0
        self.git_counter = 0
        self.cassette_library_dir = os.path.join(
            CASSETTE_LIBRARY_DIR_BASE, self.__class__.__name__, self._testMethodName
        )

        # Recording stuffs
        if RECORD:
            if os.path.exists(self.cassette_library_dir):
                shutil.rmtree(self.cassette_library_dir)
            os.makedirs(self.cassette_library_dir)

        self.recorder = vcr.VCR(
            cassette_library_dir=self.cassette_library_dir,
            record_mode="all" if RECORD else "none",
            match_on=["method", "uri"],
            filter_headers=[
                ("Authorization", "<TOKEN>"),
                ("X-Hub-Signature", "<SIGNATURE>"),
                ("User-Agent", None),
                ("Accept-Encoding", None),
                ("Connection", None),
            ],
            before_record_response=self.response_filter,
            custom_patches=(
                (pygithub.MainClass, "HTTPSConnection", vcr.stubs.VCRHTTPSConnection),
            ),
        )

        if not RECORD:
            # Never expire token during replay
            mock.patch.object(
                github_app.GithubBearerAuth, "get_or_create_jwt", return_value="<TOKEN>"
            ).start()
            mock.patch.object(
                github.GithubInstallationAuth,
                "get_access_token",
                return_value={
                    "access_token": "<TOKEN>",
                    "expires_at": (
                        datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
                    ).isoformat()
                    + "Z",
                },
            ).start()

        github_app_client = github_app._Client()

        mock.patch.object(github_app, "get_client", lambda: github_app_client).start()
        mock.patch.object(branch_updater.utils, "Gitter", self.get_gitter).start()
        mock.patch.object(duplicate_pull.utils, "Gitter", self.get_gitter).start()

        if not RECORD:
            # NOTE(sileht): Don't wait exponentialy during replay
            mock.patch.object(
                context.Context._ensure_complete.retry, "wait", None
            ).start()

        # Web authentification always pass
        mock.patch("hmac.compare_digest", return_value=True).start()

        branch_prefix_path = os.path.join(self.cassette_library_dir, "branch_prefix")

        if RECORD:
            self.BRANCH_PREFIX = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            with open(branch_prefix_path, "w") as f:
                f.write(self.BRANCH_PREFIX)
        else:
            with open(branch_prefix_path, "r") as f:
                self.BRANCH_PREFIX = f.read()

        self.master_branch_name = self.get_full_branch_name("master")

        self.git = self.get_gitter(LOG)
        self.addCleanup(self.git.cleanup)

        self.app = testclient.TestClient(web.app)

        # NOTE(sileht): Prepare a fresh redis
        self.redis = utils.get_redis_for_cache()
        self.redis.flushall()
        self.subscription = {
            "tokens": {"mergifyio-testing": config.MAIN_TOKEN},
            "subscription_active": False,
            "subscription_reason": "You're not nice",
        }
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            sub_utils.save_subscription_to_cache(
                config.INSTALLATION_ID, self.subscription,
            )
        )

        # Let's start recording
        cassette = self.recorder.use_cassette("http.json")
        cassette.__enter__()
        self.addCleanup(cassette.__exit__)

        integration = pygithub.GithubIntegration(
            config.INTEGRATION_ID, config.PRIVATE_KEY
        )
        self.installation_token = integration.get_access_token(
            config.INSTALLATION_ID
        ).token

        self.g_integration = pygithub.Github(
            self.installation_token, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        self.g_admin = pygithub.Github(
            config.MAIN_TOKEN, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )
        self.g_fork = pygithub.Github(
            config.FORK_TOKEN, base_url="https://api.%s" % config.GITHUB_DOMAIN
        )

        self.o_admin = self.g_admin.get_organization(config.TESTING_ORGANIZATION)
        self.o_integration = self.g_integration.get_organization(
            config.TESTING_ORGANIZATION
        )
        self.u_fork = self.g_fork.get_user()
        assert self.o_admin.login == "mergifyio-testing"
        assert self.o_integration.login == "mergifyio-testing"
        assert self.u_fork.login == "mergify-test2"

        # NOTE(sileht): The repository have been manually created in mergifyio-testing
        # organization and then forked in mergify-test2 user account
        self.name = "functional-testing-repo"

        self.r_o_admin = self.o_admin.get_repo(self.name)
        self.r_o_integration = self.o_integration.get_repo(self.name)
        self.r_fork = self.u_fork.get_repo(self.name)

        self.url_main = "https://%s/%s" % (
            config.GITHUB_DOMAIN,
            self.r_o_integration.full_name,
        )
        self.url_fork = "https://%s/%s/%s" % (
            config.GITHUB_DOMAIN,
            self.u_fork.login,
            self.r_o_integration.name,
        )

        installation = {"id": config.INSTALLATION_ID}
        self.cli_integration = github.get_client(
            config.TESTING_ORGANIZATION, self.name, installation
        )

        real_get_subscription = sub_utils.get_subscription

        def fake_retrieve_subscription_from_db(install_id):
            if int(install_id) == config.INSTALLATION_ID:
                return self.subscription
            else:
                return {
                    "tokens": {},
                    "subscription_active": False,
                    "subscription_reason": "We're just testing",
                }

        async def fake_subscription(r, install_id):
            if int(install_id) == config.INSTALLATION_ID:
                return await real_get_subscription(r, install_id)
            else:
                return {
                    "tokens": {},
                    "subscription_active": False,
                    "subscription_reason": "We're just testing",
                }

        mock.patch(
            "mergify_engine.branch_updater.sub_utils.get_subscription",
            side_effect=fake_subscription,
        ).start()

        mock.patch(
            "mergify_engine.branch_updater.sub_utils._retrieve_subscription_from_db",
            side_effect=fake_retrieve_subscription_from_db,
        ).start()

        mock.patch(
            "mergify_engine.sub_utils.get_subscription", new=fake_subscription,
        ).start()

        mock.patch(
            "github.MainClass.Installation.Installation.get_repos",
            return_value=[self.r_o_integration],
        ).start()

        self._event_reader = EventReader(self.app)
        self._event_reader.drain()
        self._worker_thread = WorkerThread()
        self._worker_thread.start()

    def tearDown(self):
        super(FunctionalTestBase, self).tearDown()

        # NOTE(sileht): Wait a bit to ensure all remaining events arrive. And
        # also to avoid the "git clone fork" failure that Github returns when
        # we create repo too quickly
        if RECORD:
            time.sleep(0.5)

            self.r_o_admin.edit(default_branch="master")

            for branch in self.r_o_admin.get_git_refs():
                if (
                    branch.ref.startswith("refs/heads/")
                    and branch.ref != "refs/heads/master"
                ):
                    if "branch_protection" in branch.ref:
                        self.branch_protection_unprotect(branch.ref)
                    branch.delete()

            try:
                for branch in self.r_fork.get_git_refs():
                    if (
                        branch.ref.startswith("refs/heads/")
                        and branch.ref != "refs/heads/master"
                    ):
                        branch.delete()
            except pygithub.GithubException as e:
                if e.data["message"] != "Git Repository is empty.":
                    raise

            for label in self.r_o_admin.get_labels():
                label.delete()

            for pull in self.r_o_admin.get_pulls():
                pull.close()

        self._event_reader.drain()
        self._event_reader.close()
        self._worker_thread.stop()
        mock.patch.stopall()

    def wait_for(self, *args, **kwargs):
        return self._event_reader.wait_for(*args, **kwargs)

    def get_gitter(self, logger):
        self.git_counter += 1
        return GitterRecorder(logger, self.cassette_library_dir, self.git_counter)

    def setup_repo(self, mergify_config, test_branches=[], files=[]):
        self.git("init")
        self.git.configure()
        self.git.add_cred(config.MAIN_TOKEN, "", self.r_o_integration.full_name)
        self.git.add_cred(
            config.FORK_TOKEN,
            "",
            "%s/%s" % (self.u_fork.login, self.r_o_integration.name),
        )
        self.git("config", "user.name", "%s-tester" % config.CONTEXT)
        self.git("remote", "add", "main", self.url_main)
        self.git("remote", "add", "fork", self.url_fork)

        with open(self.git.tmp + "/.mergify.yml", "w") as f:
            f.write(mergify_config)
        self.git("add", ".mergify.yml")

        if files:
            for name, content in files.items():
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                self.git("add", name)

        self.git("commit", "--no-edit", "-m", "initial commit")
        self.git("branch", "-M", self.master_branch_name)

        for test_branch in test_branches:
            self.git("branch", test_branch, self.master_branch_name)

        self.git("push", "--quiet", "main", self.master_branch_name, *test_branches)
        self.git("push", "--quiet", "fork", self.master_branch_name, *test_branches)

        self.r_o_admin.edit(default_branch=self.master_branch_name)

    @staticmethod
    def response_filter(response):
        for h in [
            "X-GitHub-Request-Id",
            "Date",
            "ETag",
            "X-RateLimit-Reset",
            "Expires",
            "Fastly-Request-ID",
            "X-Timer",
            "X-Served-By",
            "Last-Modified",
            "X-RateLimit-Remaining",
            "X-Runtime-rack",
            "Access-Control-Allow-Origin",
            "Access-Control-Expose-Headers",
            "Cache-Control",
            "Content-Security-Policy",
            "Referrer-Policy",
            "Server",
            "Status",
            "Strict-Transport-Security",
            "Vary",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "X-XSS-Protection",
        ]:
            response["headers"].pop(h, None)
            try:
                data = json.loads(response["body"]["string"].decode())
            except ValueError:
                pass
            else:
                if "token" in data:
                    data["token"] = "<TOKEN>"
                    response["body"]["string"] = json.dumps(data).encode()

        return response

    def get_full_branch_name(self, name):
        return f"{self.BRANCH_PREFIX}/{self._testMethodName}/{name}"

    def create_pr(
        self,
        base=None,
        files=None,
        two_commits=False,
        base_repo="fork",
        branch=None,
        message=None,
        draft=False,
    ):
        self.pr_counter += 1

        if base is None:
            base = self.master_branch_name

        if not branch:
            branch = "%s/pr%d" % (base_repo, self.pr_counter)
            branch = self.get_full_branch_name(branch)

        title = "Pull request n%d from %s" % (self.pr_counter, base_repo)

        self.git("checkout", "--quiet", "%s/%s" % (base_repo, base), "-b", branch)
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
            self.git("mv", "test%d" % self.pr_counter, "test%d-moved" % self.pr_counter)
            self.git("commit", "--no-edit", "-m", "%s, moved" % title)
        self.git("push", "--quiet", base_repo, branch)

        if base_repo == "fork":
            repo = self.r_fork.parent
            login = self.r_fork.owner.login
        else:
            repo = self.r_o_admin
            login = self.r_o_admin.owner.login

        p = repo.create_pull(
            base=base,
            head="%s:%s" % (login, branch),
            title=title,
            body=message or title,
            draft=draft,
        )

        self.wait_for("pull_request", {"action": "opened"})

        # NOTE(sileht): We return the same but owned by the main project
        p = self.r_o_integration.get_pull(p.number)
        commits = list(p.get_commits())

        return p, commits

    def create_status(
        self, pr, context="continuous-integration/fake-ci", state="success"
    ):
        # TODO(sileht): monkey patch PR with this
        self.r_o_admin._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={
                "state": state,
                "description": "Your change works",
                "context": context,
            },
            headers={"Accept": "application/vnd.github.machine-man-preview+json"},
        )
        self.wait_for("status", {"state": state})

    def create_review(self, pr, commit, event="APPROVE"):
        pr_review = self.r_o_admin.get_pull(pr.number)
        r = pr_review.create_review(commit, "Perfect", event=event)
        self.wait_for("pull_request_review", {"action": "submitted"})
        return r

    def create_message(self, pr, message):
        pr_review = self.r_o_admin.get_pull(pr.number)
        comment = pr_review.create_issue_comment(message)
        self.wait_for("issue_comment", {"action": "created"})
        return comment

    def add_label(self, pr, label):
        self.r_o_admin.create_label(label, "000000")
        pr.add_to_labels(label)
        self.wait_for("pull_request", {"action": "labeled"})

    def branch_protection_unprotect(self, branch):
        return self.r_o_admin._requester.requestJsonAndCheck(
            "DELETE",
            "{base_url}/branches/{branch}/protection".format(
                base_url=self.r_o_admin.url, branch=branch
            ),
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )

    def branch_protection_protect(self, branch, rule):
        if (
            self.r_o_admin.organization
            and rule["protection"]["required_pull_request_reviews"]
        ):
            rule = copy.deepcopy(rule)
            rule["protection"]["required_pull_request_reviews"][
                "dismissal_restrictions"
            ] = {}

        # NOTE(sileht): Not yet part of the API
        # maybe soon https://github.com/PyGithub/PyGithub/pull/527
        return self.r_o_admin._requester.requestJsonAndCheck(
            "PUT",
            "{base_url}/branches/{branch}/protection".format(
                base_url=self.r_o_admin.url, branch=branch
            ),
            input=rule["protection"],
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )
