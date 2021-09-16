# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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
import logging
import os
import re
import shutil
import time
import typing
import unittest
from unittest import mock
from urllib import parse

import daiquiri
import httpx
import pytest
import vcr
import vcr.stubs.urllib3_stubs

from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine import github_types
from mergify_engine import gitter
from mergify_engine import subscription
from mergify_engine import user_tokens
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http
from mergify_engine.web import root


LOG = daiquiri.getLogger(__name__)
RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = "zfixtures/cassettes"
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))


class ForwardedEvent(typing.TypedDict):
    payload: github_types.GitHubEvent
    type: github_types.GitHubEventType
    id: str


class RecordException(typing.TypedDict):
    returncode: int
    output: str


class Record(typing.TypedDict):
    args: typing.List[typing.Any]
    kwargs: typing.Dict[typing.Any, typing.Any]
    exc: RecordException
    out: str


class GitterRecorder(gitter.Gitter):
    def __init__(
        self, logger: logging.LoggerAdapter, cassette_library_dir: str, suffix: str
    ) -> None:
        super(GitterRecorder, self).__init__(logger)
        self.cassette_path = os.path.join(cassette_library_dir, f"git-{suffix}.json")
        if RECORD:
            self.records: typing.List[Record] = []
        else:
            self.load_records()

    def load_records(self) -> None:
        if not os.path.exists(self.cassette_path):
            raise RuntimeError(f"Cassette {self.cassette_path} not found")
        with open(self.cassette_path, "rb") as f:
            data = f.read().decode("utf8")
            self.records = json.loads(data)

    def save_records(self) -> None:
        with open(self.cassette_path, "wb") as f:
            data = json.dumps(self.records)
            f.write(data.encode("utf8"))

    async def __call__(self, *args, **kwargs):
        if RECORD:
            try:
                output = await super(GitterRecorder, self).__call__(*args, **kwargs)
            except gitter.GitError as e:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "exc": {
                            "returncode": e.returncode,
                            "output": e.output,
                        },
                    }
                )
                raise
            else:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "out": output,
                    }
                )
            return output
        else:
            r = self.records.pop(0)
            if "exc" in r:
                raise gitter.GitError(
                    returncode=r["exc"]["returncode"],
                    output=r["exc"]["output"],
                )
            else:
                assert r["args"] == self.prepare_args(
                    args
                ), f'{r["args"]} != {self.prepare_args(args)}'
                assert r["kwargs"] == self.prepare_kwargs(
                    kwargs
                ), f'{r["kwargs"]} != {self.prepare_kwargs(kwargs)}'
                return r["out"]

    def prepare_args(self, args):
        return [arg.replace(self.tmp, "/tmp/mergify-gitter<random>") for arg in args]

    @staticmethod
    def prepare_kwargs(kwargs):
        if "_input" in kwargs:
            kwargs["_input"] = re.sub(r"://[^@]*@", "://<TOKEN>:@", kwargs["_input"])
        return kwargs

    async def cleanup(self):
        await super(GitterRecorder, self).cleanup()
        if RECORD:
            self.save_records()


class EventReader:
    def __init__(
        self, app: httpx.AsyncClient, repository_id: github_types.GitHubRepositoryIdType
    ) -> None:
        self._app = app
        self._session = http.AsyncClient()
        self._handled_events: asyncio.Queue[ForwardedEvent] = asyncio.Queue()
        self._counter = 0

        hostname = parse.urlparse(config.GITHUB_URL).hostname
        self._namespace_endpoint = f"{config.TESTING_FORWARDER_ENDPOINT}/{hostname}/{config.INTEGRATION_ID}/{repository_id}"

    async def drain(self) -> None:
        # NOTE(sileht): Drop any pending events still on the server
        r = await self._session.request(
            "DELETE",
            self._namespace_endpoint,
            content=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        )
        r.raise_for_status()

    async def wait_for(
        self,
        event_type: github_types.GitHubEventType,
        expected_payload: typing.Any,
        timeout: float = 15 if RECORD else 2,
    ) -> None:
        LOG.log(
            42,
            "WAITING FOR %s/%s: %s",
            event_type,
            expected_payload.get("action"),
            expected_payload,
        )

        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout:
            try:
                event = self._handled_events.get_nowait()
                await self._forward_to_engine_api(event)
            except asyncio.QueueEmpty:
                for event in await self._get_events():
                    await self._handled_events.put(event)
                else:
                    if RECORD:
                        await asyncio.sleep(1)
                continue

            if event["type"] == event_type and self._match(
                event["payload"], expected_payload
            ):
                return

        raise Exception(
            f"Never got event `{event_type}` with payload `{expected_payload}` (timeout)"
        )

    def _match(self, data: github_types.GitHubEvent, expected_data: typing.Any) -> bool:
        if isinstance(expected_data, dict):
            for key, expected in expected_data.items():
                if key not in data:
                    return False
                if not self._match(data[key], expected):  # type: ignore[misc]
                    return False
            return True
        else:
            return bool(data == expected_data)

    async def _get_events(self) -> typing.List[ForwardedEvent]:
        # NOTE(sileht): we use a counter to make each call unique in cassettes
        self._counter += 1
        return typing.cast(
            typing.List[ForwardedEvent],
            (
                await self._session.request(
                    "GET",
                    f"{self._namespace_endpoint}?counter={self._counter}",
                    content=FAKE_DATA,
                    headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
                )
            ).json(),
        )

    async def _forward_to_engine_api(self, event: typing.Any) -> httpx.Response:
        payload = event["payload"]
        if event["type"] in ["check_run", "check_suite"]:
            extra = (
                f"/{payload[event['type']].get('status')}"
                f"/{payload[event['type']].get('conclusion')}"
            )
        elif event["type"] == "status":
            extra = f"/{payload.get('state')}"
        else:
            extra = ""
        LOG.log(
            42,
            "EVENT RECEIVED %s/%s%s: %s",
            event["type"],
            payload.get("action"),
            extra,
            self._remove_useless_links(copy.deepcopy(event)),
        )
        return await self._app.post(
            "/event",
            headers={
                "X-GitHub-Event": event["type"],
                "X-GitHub-Delivery": "123456789",
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
            content=json.dumps(payload),
        )

    def _remove_useless_links(self, data: typing.Any) -> typing.Any:
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
                    data[key] = self._remove_useless_links(value)
            return data
        elif isinstance(data, list):
            return [self._remove_useless_links(elem) for elem in data]
        else:
            return data


class RecordConfigType(typing.TypedDict):
    organization_id: github_types.GitHubAccountIdType
    organization_name: github_types.GitHubLogin
    repository_id: github_types.GitHubRepositoryIdType
    repository_name: github_types.GitHubRepositoryName
    branch_prefix: str


@pytest.mark.usefixtures("logger_checker")
class FunctionalTestBase(unittest.IsolatedAsyncioTestCase):
    # NOTE(sileht): The repository have been manually created in mergifyio-testing
    # organization and then forked in mergify-test2 user account
    FORK_PERSONAL_TOKEN = config.EXTERNAL_USER_PERSONAL_TOKEN
    SUBSCRIPTION_ACTIVE = False

    # To run tests on private repository, you can use:
    # FORK_PERSONAL_TOKEN = config.ORG_USER_PERSONAL_TOKEN
    # SUBSCRIPTION_ACTIVE = True

    async def asyncSetUp(self) -> None:
        super(FunctionalTestBase, self).setUp()

        # NOTE(sileht): don't preempted bucket consumption
        # Otherwise preemption doesn't occur at the same moment during record
        # and replay. Making some tests working during record and failing
        # during replay.
        config.BUCKET_PROCESSING_MAX_SECONDS = 100000

        self.existing_labels: typing.List[str] = []
        self.protected_branches: typing.Set[str] = set()
        self.pr_counter: int = 0
        self.git_counter: int = 0
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
            ignore_localhost=True,
            filter_headers=[
                ("Authorization", "<TOKEN>"),
                ("X-Hub-Signature", "<SIGNATURE>"),
                ("User-Agent", None),
                ("Accept-Encoding", None),
                ("Connection", None),
            ],
            before_record_response=self.response_filter,
        )

        if RECORD:
            github.CachedToken.STORAGE = {}
        else:
            # Never expire token during replay
            mock.patch.object(
                github_app, "get_or_create_jwt", return_value="<TOKEN>"
            ).start()
            mock.patch.object(
                github.GithubAppInstallationAuth,
                "get_access_token",
                return_value="<TOKEN>",
            ).start()

            # NOTE(sileht): httpx pyvcr stubs does not replay auth_flow as it directly patch client.send()
            # So anything occurring during auth_flow have to be mocked during replay
            def get_auth(owner_id=None, auth=None):
                if auth is None:
                    auth = github.get_auth(owner_id)
                    auth.installation = {
                        "id": config.INSTALLATION_ID,
                    }
                    auth.permissions_need_to_be_updated = False
                    auth.owner_id = config.TESTING_ORGANIZATION_ID
                    auth.owner = config.TESTING_ORGANIZATION_NAME
                return auth

            def github_aclient(owner_id=None, auth=None):
                return github.AsyncGithubInstallationClient(get_auth(owner_id, auth))

            mock.patch.object(github, "aget_client", github_aclient).start()

        mock.patch.object(branch_updater.gitter, "Gitter", self.get_gitter).start()
        mock.patch.object(duplicate_pull.gitter, "Gitter", self.get_gitter).start()

        if not RECORD:
            # NOTE(sileht): Don't wait exponentialy during replay
            mock.patch.object(
                context.Context._ensure_complete.retry, "wait", None  # type: ignore[attr-defined]
            ).start()

        # Web authentification always pass
        mock.patch("hmac.compare_digest", return_value=True).start()

        record_config_file = os.path.join(self.cassette_library_dir, "config.json")

        if RECORD:
            with open(record_config_file, "w") as f:
                f.write(
                    json.dumps(
                        RecordConfigType(
                            {
                                "organization_id": config.TESTING_ORGANIZATION_ID,
                                "organization_name": config.TESTING_ORGANIZATION_NAME,
                                "repository_id": config.TESTING_REPOSITORY_ID,
                                "repository_name": github_types.GitHubRepositoryName(
                                    config.TESTING_REPOSITORY_NAME
                                ),
                                "branch_prefix": datetime.datetime.utcnow().strftime(
                                    "%Y%m%d%H%M%S"
                                ),
                            }
                        )
                    )
                )

        with open(record_config_file, "r") as f:
            self.RECORD_CONFIG = typing.cast(RecordConfigType, json.loads(f.read()))

        self.main_branch_name = self.get_full_branch_name("main")

        self.git = self.get_gitter(LOG)
        await self.git.init()
        self.addAsyncCleanup(self.git.cleanup)

        await root.startup()
        self.app = httpx.AsyncClient(app=root.app, base_url="http://localhost")

        await self.clear_redis_cache()
        self.redis_cache = utils.create_aredis_for_cache(max_idle_time=0)
        self.subscription = subscription.Subscription(
            self.redis_cache,
            config.TESTING_ORGANIZATION_ID,
            "You're not nice",
            frozenset(
                getattr(subscription.Features, f)
                for f in subscription.Features.__members__
            )
            if self.SUBSCRIPTION_ACTIVE
            else frozenset([subscription.Features.PUBLIC_REPOSITORY]),
        )
        await self.subscription._save_subscription_to_cache()
        self.user_tokens = user_tokens.UserTokens(
            self.redis_cache,
            config.TESTING_ORGANIZATION_ID,
            [
                {
                    "login": github_types.GitHubLogin("mergify-test1"),
                    "oauth_access_token": config.ORG_ADMIN_GITHUB_APP_OAUTH_TOKEN,
                    "name": None,
                    "email": None,
                },
                {
                    "login": github_types.GitHubLogin("mergify-test3"),
                    "oauth_access_token": config.ORG_USER_PERSONAL_TOKEN,
                    "name": None,
                    "email": None,
                },
            ],
        )
        await typing.cast(
            user_tokens.UserTokensGitHubCom, self.user_tokens
        ).save_to_cache()

        # Let's start recording
        cassette = self.recorder.use_cassette("http.json")
        cassette.__enter__()
        self.addCleanup(cassette.__exit__)

        self.client_integration = github.aget_client(config.TESTING_ORGANIZATION_ID)
        self.client_admin = github.AsyncGithubInstallationClient(
            auth=github.GithubTokenAuth(token=config.ORG_ADMIN_PERSONAL_TOKEN)
        )
        self.client_fork = github.AsyncGithubInstallationClient(
            auth=github.GithubTokenAuth(token=self.FORK_PERSONAL_TOKEN)
        )
        self.addAsyncCleanup(self.client_integration.aclose)
        self.addAsyncCleanup(self.client_admin.aclose)
        self.addAsyncCleanup(self.client_fork.aclose)

        await self.client_admin.item("/user")
        await self.client_fork.item("/user")
        if RECORD:
            assert self.client_admin.auth.owner == "mergify-test1"
            assert self.client_fork.auth.owner == "mergify-test2"
            assert self.client_admin.auth.owner_id == 38494943
            assert self.client_fork.auth.owner_id == 38495008
        else:
            self.client_admin.auth.owner = github_types.GitHubLogin("mergify-test1")
            self.client_fork.auth.owner = github_types.GitHubLogin("mergify-test2")
            self.client_admin.auth.owner_id = github_types.GitHubAccountIdType(38494943)
            self.client_fork.auth.owner_id = github_types.GitHubAccountIdType(38495008)

        self.url_origin = (
            f"/repos/mergifyio-testing/{self.RECORD_CONFIG['repository_name']}"
        )
        self.url_fork = f"/repos/{self.client_fork.auth.owner}/{self.RECORD_CONFIG['repository_name']}"
        self.git_origin = f"{config.GITHUB_URL}/mergifyio-testing/{self.RECORD_CONFIG['repository_name']}"
        self.git_fork = f"{config.GITHUB_URL}/{self.client_fork.auth.owner}/{self.RECORD_CONFIG['repository_name']}"

        self.installation_ctxt = context.Installation(
            config.TESTING_ORGANIZATION_ID,
            config.TESTING_ORGANIZATION_NAME,
            self.subscription,
            self.client_integration,
            self.redis_cache,
        )
        self.repository_ctxt = await self.installation_ctxt.get_repository_by_id(
            github_types.GitHubRepositoryIdType(self.RECORD_CONFIG["repository_id"])
        )

        real_get_subscription = subscription.Subscription.get_subscription

        async def fake_retrieve_subscription_from_db(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return self.subscription
            return subscription.Subscription(
                redis_cache,
                owner_id,
                "We're just testing",
                set(subscription.Features.PUBLIC_REPOSITORY),
            )

        async def fake_subscription(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return await real_get_subscription(redis_cache, owner_id)
            return subscription.Subscription(
                redis_cache,
                owner_id,
                "We're just testing",
                set(subscription.Features.PUBLIC_REPOSITORY),
            )

        mock.patch(
            "mergify_engine.subscription.Subscription._retrieve_subscription_from_db",
            side_effect=fake_retrieve_subscription_from_db,
        ).start()

        mock.patch(
            "mergify_engine.subscription.Subscription.get_subscription",
            side_effect=fake_subscription,
        ).start()

        async def fake_retrieve_user_tokens_from_db(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return self.user_tokens
            return user_tokens.UserTokens(redis_cache, owner_id, {})

        real_get_user_tokens = user_tokens.UserTokens.get

        async def fake_user_tokens(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return await real_get_user_tokens(redis_cache, owner_id)
            return user_tokens.UserTokens(redis_cache, owner_id, {})

        mock.patch(
            "mergify_engine.user_tokens.UserTokensGitHubCom._retrieve_from_db",
            side_effect=fake_retrieve_user_tokens_from_db,
        ).start()

        mock.patch(
            "mergify_engine.user_tokens.UserTokensGitHubCom.get",
            side_effect=fake_user_tokens,
        ).start()

        # NOTE(sileht): We mock this method because when we replay test, the
        # timing maybe not the same as when we record it, making the formatted
        # elapsed time different in the merge queue summary.
        def fake_pretty_datetime(dt: datetime.datetime) -> str:
            return "<fake_pretty_datetime()>"

        mock.patch(
            "mergify_engine.date.pretty_datetime",
            side_effect=fake_pretty_datetime,
        ).start()

        self._event_reader = EventReader(self.app, self.RECORD_CONFIG["repository_id"])
        await self._event_reader.drain()

        # NOTE(sileht): Prepare a fresh redis
        await self.clear_redis_stream()

    @staticmethod
    async def clear_redis_stream() -> None:
        with utils.aredis_for_stream() as redis_stream:
            await redis_stream.flushall()

    @staticmethod
    async def clear_redis_cache() -> None:
        with utils.aredis_for_cache() as redis_stream:
            await redis_stream.flushall()

    async def asyncTearDown(self):
        await super(FunctionalTestBase, self).asyncTearDown()

        # NOTE(sileht): Wait a bit to ensure all remaining events arrive. And
        # also to avoid the "git clone fork" failure that Github returns when
        # we create repo too quickly
        if RECORD:
            await asyncio.sleep(0.5)

            await self.client_admin.patch(
                self.url_origin, json={"default_branch": "main"}
            )
            for branch in await self.get_branches():
                if branch["name"].startswith("20") or branch["name"].startswith(
                    "mergify"
                ):
                    if branch["protected"]:
                        await self.branch_protection_unprotect(branch["name"])
                    await self.client_admin.delete(
                        f"{self.url_origin}/git/refs/heads/{parse.quote(branch['name'])}"
                    )

            for label in await self.get_labels():
                await self.client_admin.delete(
                    f"{self.url_origin}/labels/{parse.quote(label['name'], safe='')}"
                )

            for pull in await self.get_pulls():
                await self.edit_pull(pull["number"], state="closed")

        await self.app.aclose()
        await root.shutdown()

        await self._event_reader.drain()
        await self.clear_redis_stream()
        mock.patch.stopall()

    async def wait_for(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        return await self._event_reader.wait_for(*args, **kwargs)

    @staticmethod
    async def _async_run_workers(timeout: float) -> None:
        w = worker.Worker(
            idle_sleep_time=0.42 if RECORD else 0.01,
            enabled_services={"stream", "delayed-refresh"},
        )
        await w.start()

        started_at = None
        while True:
            if w._redis_stream is None or (await w._redis_stream.zcard("streams")) > 0:
                started_at = None
            elif started_at is None:
                started_at = time.monotonic()
            elif time.monotonic() - started_at >= timeout:
                break
            await asyncio.sleep(timeout)

        w.stop()
        await w.wait_shutdown_complete()

    async def run_engine(self, timeout: float = 0.42 if RECORD else 0.02) -> None:
        LOG.log(42, "RUNNING ENGINE")
        await self._async_run_workers(timeout)

    def get_gitter(self, logger: logging.LoggerAdapter) -> GitterRecorder:
        self.git_counter += 1
        return GitterRecorder(logger, self.cassette_library_dir, str(self.git_counter))

    async def setup_repo(
        self,
        mergify_config: typing.Optional[str] = None,
        test_branches: typing.Optional[typing.Iterable[str]] = None,
        files: typing.Optional[typing.Dict[str, str]] = None,
    ) -> None:

        if self.git.tmp is None:
            raise RuntimeError("self.git.init() not called, tmp dir empty")

        if test_branches is None:
            test_branches = []
        if files is None:
            files = {}

        await self.git.configure()
        await self.git.add_cred(
            config.ORG_ADMIN_PERSONAL_TOKEN,
            "",
            f"mergifyio-testing/{self.RECORD_CONFIG['repository_name']}",
        )
        await self.git.add_cred(
            self.FORK_PERSONAL_TOKEN,
            "",
            f"{self.client_fork.auth.owner}/{self.RECORD_CONFIG['repository_name']}",
        )
        await self.git("config", "user.name", f"{config.CONTEXT}-tester")
        await self.git("remote", "add", "origin", self.git_origin)
        await self.git("remote", "add", "fork", self.git_fork)

        if mergify_config is None:
            with open(self.git.tmp + "/.gitkeep", "w") as f:
                f.write("repo must not be empty")
            await self.git("add", ".gitkeep")
        else:
            with open(self.git.tmp + "/.mergify.yml", "w") as f:
                f.write(mergify_config)
            await self.git("add", ".mergify.yml")

        if files:
            await self._git_create_files(files)

        await self.git("commit", "--no-edit", "-m", "initial commit")
        await self.git("branch", "-M", self.main_branch_name)

        for test_branch in test_branches:
            await self.git("branch", test_branch, self.main_branch_name)

        await self.git(
            "push", "--quiet", "origin", self.main_branch_name, *test_branches
        )
        await self.git("push", "--quiet", "fork", self.main_branch_name, *test_branches)

        await self.client_admin.patch(
            self.url_origin, json={"default_branch": self.main_branch_name}
        )

    @staticmethod
    def response_filter(response):
        for h in [
            "CF-Cache-Status",
            "CF-RAY",
            "Expect-CT",
            "Report-To",
            "NEL",
            "cf-request-id",
            "Via",
            "X-GitHub-Request-Id",
            "Date",
            "ETag",
            "X-RateLimit-Reset",
            "X-RateLimit-Used",
            "X-RateLimit-Resource",
            "X-RateLimit-Limit",
            "Via",
            "cookie",
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

        if "body" in response:
            # Urllib3 vcrpy format
            try:
                data = json.loads(response["body"]["string"].decode())
            except ValueError:
                data = None
        else:
            # httpx vcrpy format
            try:
                data = json.loads(response["content"])
            except ValueError:
                data = None

        if data and "token" in data:
            data["token"] = "<TOKEN>"
            if "body" in response:
                # Urllib3 vcrpy format
                response["body"]["string"] = json.dumps(data).encode()
            else:
                # httpx vcrpy format
                response["content"] = json.dumps(data)

        return response

    def get_full_branch_name(self, name: str) -> str:
        return f"{self.RECORD_CONFIG['branch_prefix']}/{self._testMethodName}/{name}"

    async def create_pr(
        self,
        base: typing.Optional[str] = None,
        files: typing.Optional[typing.Dict[str, str]] = None,
        two_commits: bool = False,
        base_repo: typing.Literal["origin", "fork"] = "fork",
        branch: typing.Optional[str] = None,
        message: typing.Optional[str] = None,
        draft: bool = False,
    ) -> typing.Tuple[
        github_types.GitHubPullRequest, typing.List[github_types.GitHubBranchCommit]
    ]:
        self.pr_counter += 1

        if self.git.tmp is None:
            raise RuntimeError("self.git.init() not called, tmp dir empty")

        if base is None:
            base = self.main_branch_name

        if not branch:
            branch = f"{base_repo}/pr{self.pr_counter}"
            branch = self.get_full_branch_name(branch)

        title = (
            f"{self._testMethodName}: pull request n{self.pr_counter} from {base_repo}"
        )

        await self.git("checkout", "--quiet", f"{base_repo}/{base}", "-b", branch)
        if files:
            await self._git_create_files(files)
        else:
            open(self.git.tmp + f"/test{self.pr_counter}", "wb").close()
            await self.git("add", f"test{self.pr_counter}")
        await self.git("commit", "--no-edit", "-m", title)
        if two_commits:
            await self.git(
                "mv", f"test{self.pr_counter}", f"test{self.pr_counter}-moved"
            )
            await self.git("commit", "--no-edit", "-m", f"{title}, moved")
        await self.git("push", "--quiet", base_repo, branch)

        if base_repo == "fork":
            client = self.client_fork
            login = self.client_fork.auth.owner
        else:
            client = self.client_admin
            login = github_types.GitHubLogin("mergifyio-testing")

        resp = await client.post(
            f"{self.url_origin}/pulls",
            json={
                "base": base,
                "head": f"{login}:{branch}",
                "title": title,
                "body": message or title,
                "draft": draft,
            },
        )
        await self.wait_for("pull_request", {"action": "opened"})

        # NOTE(sileht): We return the same but owned by the main project
        p = typing.cast(github_types.GitHubPullRequest, resp.json())
        p = await self.get_pull(p["number"])
        commits = await self.get_commits(p["number"])
        return p, commits

    async def _git_create_files(self, files: typing.Dict[str, str]) -> None:
        if self.git.tmp is None:
            raise RuntimeError("self.git.init() not called, tmp dir empty")

        for name, content in files.items():
            path = self.git.tmp + "/" + name
            directory_path = os.path.dirname(path)
            os.makedirs(directory_path, exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            await self.git("add", name)

    async def create_status(
        self,
        pull: github_types.GitHubPullRequest,
        context: str = "continuous-integration/fake-ci",
        state: github_types.GitHubStatusState = "success",
    ) -> None:
        await self.client_admin.post(
            f"{self.url_origin}/statuses/{pull['head']['sha']}",
            json={
                "state": state,
                "description": f"The CI is {state}",
                "context": context,
            },
        )
        await self.wait_for("status", {"state": state})

    async def create_review(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
        event: typing.Literal[
            "APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING"
        ] = "APPROVE",
    ) -> None:
        await self.client_admin.post(
            f"{self.url_origin}/pulls/{pull_number}/reviews",
            json={"event": event, "body": f"event: {event}"},
        )
        await self.wait_for("pull_request_review", {"action": "submitted"})

    async def get_review_requests(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
    ) -> github_types.GitHubRequestedReviewers:
        return typing.cast(
            github_types.GitHubRequestedReviewers,
            await self.client_admin.item(
                f"{self.url_origin}/pulls/{pull_number}/requested_reviewers",
            ),
        )

    async def create_review_request(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
        reviewers: typing.List[str],
    ) -> None:
        await self.client_admin.post(
            f"{self.url_origin}/pulls/{pull_number}/requested_reviewers",
            json={"reviewers": reviewers},
        )
        await self.wait_for("pull_request", {"action": "review_requested"})

    async def create_comment(
        self, pull_number: github_types.GitHubPullRequestNumber, message: str
    ) -> None:
        await self.client_admin.post(
            f"{self.url_origin}/issues/{pull_number}/comments", json={"body": message}
        )
        await self.wait_for("issue_comment", {"action": "created"})

    async def create_issue(self, title: str, body: str) -> github_types.GitHubIssue:
        resp = await self.client_admin.post(
            f"{self.url_origin}/issues", json={"body": body, "title": title}
        )
        # NOTE(sileht):Our GitHubApp doesn't subscribe to issues event
        # await self.wait_for("issues", {"action": "created"})
        return typing.cast(github_types.GitHubIssue, resp.json())

    async def add_assignee(
        self, pull_number: github_types.GitHubPullRequestNumber, assignee: str
    ) -> None:
        await self.client_admin.post(
            f"{self.url_origin}/issues/{pull_number}/assignees",
            json={"assignees": [assignee]},
        )
        await self.wait_for("pull_request", {"action": "assigned"})

    async def add_label(
        self, pull_number: github_types.GitHubPullRequestNumber, label: str
    ) -> None:
        if label not in self.existing_labels:
            try:
                await self.client_admin.post(
                    f"{self.url_origin}/labels", json={"name": label, "color": "000000"}
                )
            except http.HTTPClientSideError as e:
                if e.status_code != 422:
                    raise

            self.existing_labels.append(label)

        await self.client_admin.post(
            f"{self.url_origin}/issues/{pull_number}/labels", json={"labels": [label]}
        )
        await self.wait_for("pull_request", {"action": "labeled"})

    async def remove_label(
        self, pull_number: github_types.GitHubPullRequestNumber, label: str
    ) -> None:
        await self.client_admin.delete(
            f"{self.url_origin}/issues/{pull_number}/labels/{label}"
        )
        await self.wait_for("pull_request", {"action": "unlabeled"})

    async def branch_protection_unprotect(self, branch: str) -> None:
        await self.client_admin.delete(
            f"{self.url_origin}/branches/{branch}/protection",
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )
        self.protected_branches.remove(branch)

    async def branch_protection_protect(
        self, branch: str, protection: typing.Dict[str, typing.Any]
    ) -> None:
        if protection["required_pull_request_reviews"]:
            protection = copy.deepcopy(protection)
            protection["required_pull_request_reviews"]["dismissal_restrictions"] = {}

        await self.client_admin.put(
            f"{self.url_origin}/branches/{branch}/protection",
            json=protection,
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )
        self.protected_branches.add(branch)

    async def get_branches(self) -> typing.List[github_types.GitHubBranch]:
        return [c async for c in self.client_admin.items(f"{self.url_origin}/branches")]

    async def get_commits(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> typing.List[github_types.GitHubBranchCommit]:
        return [
            c
            async for c in typing.cast(
                typing.AsyncGenerator[github_types.GitHubBranchCommit, None],
                self.client_admin.items(
                    f"{self.url_origin}/pulls/{pull_number}/commits"
                ),
            )
        ]

    async def get_head_commit(self) -> github_types.GitHubBranchCommit:
        return typing.cast(
            github_types.GitHubBranch,
            await self.client_admin.item(
                f"{self.url_origin}/branches/{self.main_branch_name}"
            ),
        )["commit"]

    async def get_issue_comments(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> typing.List[github_types.GitHubComment]:
        return [
            comment
            async for comment in typing.cast(
                typing.AsyncGenerator[github_types.GitHubComment, None],
                self.client_admin.items(
                    f"{self.url_origin}/issues/{pull_number}/comments"
                ),
            )
        ]

    async def get_reviews(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> typing.List[github_types.GitHubReview]:
        return [
            review
            async for review in typing.cast(
                typing.AsyncGenerator[github_types.GitHubReview, None],
                self.client_admin.items(
                    f"{self.url_origin}/pulls/{pull_number}/reviews"
                ),
            )
        ]

    async def get_pull(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> github_types.GitHubPullRequest:
        return typing.cast(
            github_types.GitHubPullRequest,
            await self.client_admin.item(f"{self.url_origin}/pulls/{pull_number}"),
        )

    async def get_pulls(
        self,
        **kwargs: typing.Any,
    ) -> typing.List[github_types.GitHubPullRequest]:
        return [
            i
            async for i in self.client_admin.items(f"{self.url_origin}/pulls", **kwargs)
        ]

    async def edit_pull(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
        **payload: typing.Dict[str, typing.Any],
    ) -> github_types.GitHubPullRequest:
        return typing.cast(
            github_types.GitHubPullRequest,
            (
                await self.client_admin.patch(
                    f"{self.url_origin}/pulls/{pull_number}", json=payload
                )
            ).json(),
        )

    async def is_pull_merged(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
    ) -> bool:
        try:
            await self.client_admin.get(f"{self.url_origin}/pulls/{pull_number}/merge")
        except http.HTTPNotFound:
            return False
        else:
            return True

    async def merge_pull(
        self,
        pull_number: github_types.GitHubPullRequestNumber,
    ) -> None:
        await self.client_admin.put(f"{self.url_origin}/pulls/{pull_number}/merge")

    async def get_labels(self) -> typing.List[github_types.GitHubLabel]:
        return [
            label
            async for label in typing.cast(
                typing.AsyncGenerator[github_types.GitHubLabel, None],
                self.client_admin.items(f"{self.url_origin}/labels"),
            )
        ]

    async def find_git_refs(
        self, url: str, matches: typing.List[str]
    ) -> typing.AsyncGenerator[github_types.GitHubGitRef, None]:
        for match in matches:
            async for matchedBranch in typing.cast(
                typing.AsyncGenerator[github_types.GitHubGitRef, None],
                self.client_admin.items(f"{url}/git/matching-refs/heads/{match}"),
            ):
                yield matchedBranch

    async def get_teams(self) -> typing.List[github_types.GitHubTeam]:
        return [
            t
            async for t in typing.cast(
                typing.AsyncGenerator[github_types.GitHubTeam, None],
                self.client_admin.items("/orgs/mergifyio-testing/teams"),
            )
        ]
