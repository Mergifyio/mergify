# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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

import base64
import typing

import pytest
import respx

from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import engine
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.clients import github
from mergify_engine.dashboard import subscription


FAKE_MERGIFY_CONTENT = base64.b64encode(b"pull_request_rules:").decode()
OTHER_FAKE_MERGIFY_CONTENT = base64.b64encode(b"whatever:").decode()

GH_OWNER = github_types.GitHubAccount(
    {
        "login": github_types.GitHubLogin("testing"),
        "id": github_types.GitHubAccountIdType(12345),
        "type": "User",
        "avatar_url": "",
    }
)

GH_REPO = github_types.GitHubRepository(
    {
        "archived": False,
        "url": "",
        "html_url": "",
        "default_branch": github_types.GitHubRefType("main"),
        "id": github_types.GitHubRepositoryIdType(456),
        "full_name": "user/ref",
        "name": github_types.GitHubRepositoryName("name"),
        "private": False,
        "owner": GH_OWNER,
    }
)
GH_PULL = github_types.GitHubPullRequest(
    {
        "node_id": "42",
        "locked": False,
        "assignees": [],
        "requested_reviewers": [],
        "requested_teams": [],
        "milestone": None,
        "title": "",
        "body": "",
        "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
        "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
        "closed_at": None,
        "id": github_types.GitHubPullRequestId(0),
        "maintainer_can_modify": False,
        "rebaseable": False,
        "draft": False,
        "merge_commit_sha": github_types.SHAType("base-sha"),
        "labels": [],
        "number": github_types.GitHubPullRequestNumber(6),
        "merged": False,
        "commits": 1,
        "state": "open",
        "html_url": "<html_url>",
        "base": {
            "label": github_types.GitHubBaseBranchLabel(""),
            "sha": github_types.SHAType("base-sha"),
            "user": {
                "login": github_types.GitHubLogin("owner"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
                "avatar_url": "",
            },
            "ref": github_types.GitHubRefType("main"),
            "repo": GH_REPO,
        },
        "head": {
            "label": github_types.GitHubHeadBranchLabel(""),
            "sha": github_types.SHAType("old-sha-one"),
            "ref": github_types.GitHubRefType("fork"),
            "user": {
                "login": github_types.GitHubLogin("user"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
                "avatar_url": "",
            },
            "repo": {
                "archived": False,
                "url": "",
                "html_url": "",
                "default_branch": github_types.GitHubRefType(""),
                "id": github_types.GitHubRepositoryIdType(123),
                "full_name": "fork/other",
                "name": github_types.GitHubRepositoryName("other"),
                "private": False,
                "owner": {
                    "login": github_types.GitHubLogin("user"),
                    "id": github_types.GitHubAccountIdType(0),
                    "type": "User",
                    "avatar_url": "",
                },
            },
        },
        "user": {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        },
        "merged_by": None,
        "merged_at": None,
        "mergeable_state": "clean",
        "mergeable": True,
        "changed_files": 300,
    },
)

CHECK_RUN = github_types.GitHubCheckRun(
    {
        "head_sha": github_types.SHAType("ce587453ced02b1526dfb4cb910479d431683101"),
        "details_url": "https://example.com",
        "status": "completed",
        "conclusion": "neutral",
        "name": "neutral",
        "id": 1236,
        "app": {
            "id": 1234,
            "name": "CI",
            "owner": {
                "type": "User",
                "id": github_types.GitHubAccountIdType(1234),
                "login": github_types.GitHubLogin("goo"),
                "avatar_url": "https://example.com",
            },
        },
        "external_id": "",
        "pull_requests": [],
        "before": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "after": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "started_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "completed_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "html_url": "https://example.com",
        "check_suite": {"id": 1234},
        "output": {
            "summary": "",
            "title": "It runs!",
            "text": "",
            "annotations": [],
            "annotations_count": 0,
            "annotations_url": "https://example.com",
        },
    }
)

SUMMARY_CHECK = github_types.GitHubCheckRun(
    {
        "id": 123,
        "name": constants.SUMMARY_NAME,
        "head_sha": GH_PULL["head"]["sha"],
        "output": {
            "title": "whatever",
            "summary": "whatever",
            "annotations": [],
            "annotations_count": 0,
            "annotations_url": "",
            "text": "",
        },
        "pull_requests": [],
        "status": "completed",
        "conclusion": "success",
        "before": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "after": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "started_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "completed_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "html_url": "https://example.com",
        "check_suite": {"id": 1234},
        "app": {
            "id": config.INTEGRATION_ID,
            "name": "Mergify",
            "owner": {
                "type": "Bot",
                "id": github_types.GitHubAccountIdType(config.BOT_USER_ID),
                "login": github_types.GitHubLogin(config.BOT_USER_LOGIN),
                "avatar_url": "https://example.com",
            },
        },
        "details_url": "",
        "external_id": "",
    }
)

CONFIGURATION_DELETED_CHECK = github_types.GitHubCheckRun(
    {
        "id": 123,
        "name": constants.CONFIGURATION_DELETED_CHECK_NAME,
        "head_sha": GH_PULL["head"]["sha"],
        "output": {
            "title": "Configuration deleted!",
            "summary": "whatever",
            "annotations": [],
            "annotations_count": 0,
            "annotations_url": "",
            "text": "",
        },
        "pull_requests": [],
        "status": "completed",
        "conclusion": "success",
        "before": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "after": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "started_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "completed_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "html_url": "https://example.com",
        "check_suite": {"id": 1234},
        "app": {
            "id": config.INTEGRATION_ID,
            "name": "Mergify",
            "owner": {
                "type": "Bot",
                "id": github_types.GitHubAccountIdType(config.BOT_USER_ID),
                "login": github_types.GitHubLogin(config.BOT_USER_LOGIN),
                "avatar_url": "https://example.com",
            },
        },
        "details_url": "",
        "external_id": "",
    }
)

CONFIGURATION_CHANGED_CHECK = github_types.GitHubCheckRun(
    {
        "id": 123,
        "name": constants.CONFIGURATION_CHANGED_CHECK_NAME,
        "head_sha": GH_PULL["head"]["sha"],
        "output": {
            "title": "Configuration chcanged!",
            "summary": "whatever",
            "annotations": [],
            "annotations_count": 0,
            "annotations_url": "",
            "text": "",
        },
        "pull_requests": [],
        "status": "completed",
        "conclusion": "success",
        "before": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "after": github_types.SHAType("4eef79d038b0327a5e035fd65059e556a55c6aa4"),
        "started_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "completed_at": github_types.ISODateTimeType("2004-12-02T22:00"),
        "html_url": "https://example.com",
        "check_suite": {"id": 1234},
        "app": {
            "id": config.INTEGRATION_ID,
            "name": "Mergify",
            "owner": {
                "type": "Bot",
                "id": github_types.GitHubAccountIdType(config.BOT_USER_ID),
                "login": github_types.GitHubLogin(config.BOT_USER_LOGIN),
                "avatar_url": "https://example.com",
            },
        },
        "details_url": "",
        "external_id": "",
    }
)

BASE_URL = f"/repos/{GH_OWNER['login']}/{GH_REPO['name']}"


async def test_configuration_changed(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )

    qs_ref = respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & ~qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": OTHER_FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "ab739e5ec79e358bae7a150941a148b4131233ce"
                    ),
                }
            ),
        ),
    )

    github_server.get(
        f"{BASE_URL}/contents/.github/mergify.yml",
        params__contains={"ref": GH_PULL["merge_commit_sha"]},
    ).respond(404, json={})
    github_server.get(
        f"{BASE_URL}/contents/.mergify/config.yml",
        params__contains={"ref": GH_PULL["merge_commit_sha"]},
    ).respond(404, json={})

    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(200, json={"check_runs": []})

    github_server.post(f"{BASE_URL}/check-runs").respond(
        200, json=typing.cast(typing.Dict[typing.Any, typing.Any], CHECK_RUN)
    )

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        assert main_config_file is not None
        assert main_config_file["decoded_content"] == "pull_request_rules:"

        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert changed


async def test_configuration_duplicated(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )

    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )

    qs_ref = respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & ~qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    github_server.get(
        f"{BASE_URL}/contents/.mergify/config.yml",
        params__contains={"ref": GH_PULL["merge_commit_sha"]},
    ).respond(404)

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.github/mergify.yml")
        & qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": OTHER_FAKE_MERGIFY_CONTENT,
                    "path": ".github/mergify.yml",
                    "sha": github_types.SHAType(
                        "ab739e5ec79e358bae7a150941a148b4131233ce"
                    ),
                }
            ),
        ),
    )

    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(200, json={"check_runs": []})

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        assert main_config_file is not None
        assert main_config_file["decoded_content"] == "pull_request_rules:"

        with pytest.raises(engine.MultipleConfigurationFileFound):
            await engine._check_configuration_changes(ctxt, main_config_file)


async def test_configuration_not_changed(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )

    qs_ref = respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & ~qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & qs_ref
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    github_server.get(
        f"{BASE_URL}/contents/.mergify/config.yml",
        params__contains={"ref": GH_PULL["merge_commit_sha"]},
    ).respond(404)

    github_server.get(
        f"{BASE_URL}/contents/.github/mergify.yml",
        params__contains={"ref": GH_PULL["merge_commit_sha"]},
    ).respond(404)

    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(200, json={"check_runs": []})

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        assert main_config_file is not None
        assert main_config_file["decoded_content"] == "pull_request_rules:"

        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert not changed


async def test_configuration_initial(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & ~respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(404)

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify/config.yml")
        & ~respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(404)

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.github/mergify.yml")
        & ~respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(404)

    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify.yml")
        & respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )
    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.github/mergify.yml")
        & respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(404)
    github_server.route(
        respx.patterns.M(method="GET", path=f"{BASE_URL}/contents/.mergify/config.yml")
        & respx.patterns.M(params__contains={"ref": GH_PULL["merge_commit_sha"]})
    ).respond(404)

    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(200, json={"check_runs": []})

    github_server.post(f"{BASE_URL}/check-runs").respond(
        200, json=typing.cast(typing.Dict[typing.Any, typing.Any], CHECK_RUN)
    )

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        assert main_config_file is None

        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert changed


async def test_configuration_check_not_needed_with_configuration_not_changed(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )
    github_server.get(f"{BASE_URL}/contents/.mergify.yml").respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    # Summary is present, no need to redo the check
    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(
        200,
        json={"check_runs": [SUMMARY_CHECK]},
    )

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert not changed


async def test_configuration_check_not_needed_with_configuration_changed(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(
        f"{BASE_URL}/pulls/1",
    ).respond(200, json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL))
    github_server.get(f"{BASE_URL}/contents/.mergify.yml").respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    # Summary is present, no need to redo the check
    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(
        200,
        json={"check_runs": [SUMMARY_CHECK, CONFIGURATION_CHANGED_CHECK]},
    )

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert changed


async def test_configuration_check_not_needed_with_configuration_deleted(
    github_server: respx.MockRouter, redis_links: redis_utils.RedisLinks
) -> None:
    github_server.get("/user/12345/installation").respond(
        200,
        json={
            "id": 12345,
            "permissions": {
                "checks": "write",
                "contents": "write",
                "pull_requests": "write",
            },
            "target_type": GH_OWNER["type"],
            "account": GH_OWNER,
        },
    )
    github_server.get(f"{BASE_URL}/pulls/1",).respond(
        200,
        json=typing.cast(typing.Dict[typing.Any, typing.Any], GH_PULL),
    )
    github_server.get(f"{BASE_URL}/contents/.mergify.yml").respond(
        200,
        json=typing.cast(
            typing.Dict[typing.Any, typing.Any],
            github_types.GitHubContentFile(
                {
                    "type": "file",
                    "content": FAKE_MERGIFY_CONTENT,
                    "path": ".mergify.yml",
                    "sha": github_types.SHAType(
                        "739e5ec79e358bae7a150941a148b4131233ce2c"
                    ),
                }
            ),
        ),
    )

    # Summary is present, no need to redo the check
    github_server.get(
        f"{BASE_URL}/commits/{GH_PULL['head']['sha']}/check-runs"
    ).respond(
        200,
        json={"check_runs": [SUMMARY_CHECK, CONFIGURATION_DELETED_CHECK]},
    )

    installation_json = await github.get_installation_from_account_id(GH_OWNER["id"])
    async with github.AsyncGithubInstallationClient(
        github.GithubAppInstallationAuth(installation_json)
    ) as client:
        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_links.cache,
                0,
                "",
                frozenset([subscription.Features.PUBLIC_REPOSITORY]),
                0,
            ),
            client,
            redis_links,
        )
        repository = context.Repository(installation, GH_REPO)
        ctxt = await repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(1)
        )

        main_config_file = await repository.get_mergify_config_file()
        changed = await engine._check_configuration_changes(ctxt, main_config_file)
        assert changed
