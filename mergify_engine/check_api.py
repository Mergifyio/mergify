# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
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

import dataclasses
import datetime
import enum
import typing

from mergify_engine import config
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import utils
from mergify_engine.clients import http


if typing.TYPE_CHECKING:
    from mergify_engine import context


# Used to track check run created by Mergify but for the user via the checks action
# e.g.: we want the engine to be retriggered if the state of this kind of checks changes.
USER_CREATED_CHECKS = "user-created-checkrun"


class GitHubCheckRunOutputParameters(typing.TypedDict, total=False):
    title: str
    summary: str
    text: typing.Optional[str]
    annotations: typing.Optional[typing.List[github_types.GitHubAnnotation]]


class GitHubCheckRunParameters(typing.TypedDict, total=False):
    external_id: str
    head_sha: github_types.SHAType
    name: str
    status: github_types.GitHubCheckRunStatus
    output: GitHubCheckRunOutputParameters
    conclusion: typing.Optional[github_types.GitHubCheckRunConclusion]
    completed_at: github_types.ISODateTimeType
    started_at: github_types.ISODateTimeType
    details_url: str


class Status(enum.Enum):
    # TODO(sileht): Since we have mypy, this enum is a bit useless, we can replace it
    # with github_types.GitHubCheckRunStatus
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Conclusion(enum.Enum):
    PENDING = None
    CANCELLED = "cancelled"
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"
    STALE = "stale"
    ACTION_REQUIRED = "action_required"

    @staticmethod
    def _normalize(value: str) -> str:
        """Return normalized value."""
        return value.lower().replace("_", " ")

    @property
    def emoji(self) -> typing.Optional[str]:
        if self.value is None:
            return "🟠"
        elif self.value == "success":
            return "✅"
        elif self.value == "failure":
            return "❌"
        elif self.value in ("skipped", "neutral", "stale"):
            return "☑️"
        elif self.value == "action_required":
            return "⚠️"

        return None

    def __str__(self) -> str:
        prefix = self.emoji
        if prefix is None:
            prefix = ""
        else:
            prefix = prefix + " "

        return prefix + self._normalize(self.name)


json.register_type(Conclusion)


@dataclasses.dataclass
class Result:
    conclusion: Conclusion
    title: str
    summary: str
    annotations: typing.Optional[typing.List[github_types.GitHubAnnotation]] = None
    started_at: typing.Optional[datetime.datetime] = None
    ended_at: typing.Optional[datetime.datetime] = None


def to_check_run_light(
    check: github_types.GitHubCheckRun,
) -> github_types.CachedGitHubCheckRun:
    if check["app"]["id"] != config.INTEGRATION_ID:
        # NOTE(sileht): We only need the output for our own checks
        check["output"]["text"] = None
        check["output"]["summary"] = ""
        check["output"]["annotations"] = []
        check["output"]["annotations_url"] = ""

    return github_types.CachedGitHubCheckRun(
        {
            "id": check["id"],
            "app_id": check["app"]["id"],
            "app_name": check["app"]["name"],
            "app_avatar_url": check["app"]["owner"]["avatar_url"],
            "external_id": check["external_id"],
            "head_sha": check["head_sha"],
            "name": check["name"],
            "status": check["status"],
            "output": check["output"],
            "conclusion": check["conclusion"],
            "completed_at": check["completed_at"],
            "html_url": check["html_url"],
        }
    )


async def get_checks_for_ref(
    ctxt: "context.Context",
    sha: github_types.SHAType,
    check_name: typing.Optional[str] = None,
    app_id: typing.Optional[int] = None,
) -> typing.List[github_types.CachedGitHubCheckRun]:
    if check_name is None:
        params = {}
    else:
        params = {"check_name": check_name}

    if app_id is not None:
        params["app_id"] = str(app_id)

    try:
        checks = [
            to_check_run_light(check)
            async for check in typing.cast(
                typing.AsyncGenerator[github_types.GitHubCheckRun, None],
                ctxt.client.items(
                    f"{ctxt.base_url}/commits/{sha}/check-runs",
                    resource_name="check runs",
                    page_limit=5,
                    api_version="antiope",
                    list_items="check_runs",
                    params=params,
                ),
            )
        ]
    except http.HTTPClientSideError as e:
        if e.status_code == 422 and "No commit found for SHA" in e.message:
            return []
        raise

    return checks


_K = typing.TypeVar("_K")
_V = typing.TypeVar("_V")


def compare_dict(
    d1: typing.Dict[_K, _V], d2: typing.Dict[_K, _V], keys: typing.Iterable[_K]
) -> bool:
    for key in keys:
        if d1.get(key) != d2.get(key):
            return False
    return True


def check_need_update(
    previous_check: github_types.CachedGitHubCheckRun,
    expected_check: GitHubCheckRunParameters,
) -> bool:
    if compare_dict(
        typing.cast(typing.Dict[str, typing.Any], expected_check),
        typing.cast(typing.Dict[str, typing.Any], previous_check),
        ("head_sha", "status", "conclusion", "details_url"),
    ):
        if previous_check["output"] is None and expected_check["output"] is None:
            return False
        elif previous_check["output"] is not None and compare_dict(
            typing.cast(typing.Dict[str, typing.Any], expected_check["output"]),
            typing.cast(typing.Dict[str, typing.Any], previous_check["output"]),
            ("title", "summary"),
        ):
            return False

    return True


async def set_check_run(
    ctxt: "context.Context",
    name: str,
    result: Result,
    external_id: typing.Optional[str] = None,
    skip_cache: bool = False,
) -> github_types.CachedGitHubCheckRun:
    if result.conclusion is Conclusion.PENDING:
        status = Status.IN_PROGRESS
    else:
        status = Status.COMPLETED

    started_at = (result.started_at or date.utcnow()).isoformat()

    post_parameters = GitHubCheckRunParameters(
        {
            "name": name,
            "head_sha": ctxt.pull["head"]["sha"],
            "status": typing.cast(github_types.GitHubCheckRunStatus, status.value),
            "started_at": typing.cast(github_types.ISODateTimeType, started_at),
            "details_url": f"{ctxt.pull['html_url']}/checks",
            "output": {
                "title": result.title,
                "summary": result.summary,
            },
        }
    )

    if result.annotations is not None:
        post_parameters["output"]["annotations"] = result.annotations

    # Maximum output/summary length for Check API is 65535
    summary = post_parameters["output"]["summary"]
    if summary:
        post_parameters["output"]["summary"] = utils.unicode_truncate(
            summary, 65535, "…"
        )

    if external_id:
        post_parameters["external_id"] = external_id

    if status is Status.COMPLETED:
        ended_at = (result.ended_at or date.utcnow()).isoformat()
        post_parameters["conclusion"] = result.conclusion.value
        post_parameters["completed_at"] = typing.cast(
            github_types.ISODateTimeType, ended_at
        )

    if skip_cache:
        checks = sorted(
            await get_checks_for_ref(
                ctxt,
                ctxt.pull["head"]["sha"],
                check_name=name,
                app_id=config.INTEGRATION_ID,
            ),
            key=lambda c: c["id"],
            reverse=True,
        )
    else:
        checks = sorted(
            (c for c in await ctxt.pull_engine_check_runs if c["name"] == name),
            key=lambda c: c["id"],
            reverse=True,
        )

    if len(checks) >= 2:
        ctxt.log.warning(
            "pull requests with duplicate checks",
            checks=checks,
            skip_cache=skip_cache,
            all_checks=await ctxt.pull_engine_check_runs,
            fresh_checks=await get_checks_for_ref(
                ctxt, ctxt.pull["head"]["sha"], app_id=config.INTEGRATION_ID
            ),
        )

    if not checks or (
        Status(checks[0]["status"]) == Status.COMPLETED and status == Status.IN_PROGRESS
    ):
        # NOTE(sileht): First time we see it, or the previous one have been completed and
        # now go back to in_progress. Since GitHub doesn't allow to change status of
        # completed check-runs, we have to create a new one.
        new_check = to_check_run_light(
            typing.cast(
                github_types.GitHubCheckRun,
                (
                    await ctxt.client.post(
                        f"{ctxt.base_url}/check-runs",
                        api_version="antiope",
                        json=post_parameters,
                    )
                ).json(),
            )
        )
    else:
        post_parameters["details_url"] += f"?check_run_id={checks[0]['id']}"

        # Don't do useless update
        if check_need_update(checks[0], post_parameters):
            new_check = to_check_run_light(
                typing.cast(
                    github_types.GitHubCheckRun,
                    (
                        await ctxt.client.patch(
                            f"{ctxt.base_url}/check-runs/{checks[0]['id']}",
                            api_version="antiope",
                            json=post_parameters,
                        )
                    ).json(),
                )
            )
        else:
            new_check = checks[0]

    if not skip_cache:
        await ctxt.update_cached_check_runs(new_check)
    return new_check
