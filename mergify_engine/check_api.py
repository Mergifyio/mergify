# -*- encoding: utf-8 -*-
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
import enum
import typing

from mergify_engine import github_types
from mergify_engine import utils


if typing.TYPE_CHECKING:
    from mergify_engine import context


# Used to track check run created by Mergify but for the user via the checks action
# e.g.: we want the engine to be retriggered if the state of this kind of checks changes.
USER_CREATED_CHECKS = "user-created-checkrun"


class GitHubCheckRunOutputParameters(typing.TypedDict, total=False):
    title: str
    summary: str
    text: typing.Optional[str]
    annotations: typing.Optional[typing.List[str]]


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
    NEUTRAL = "neutral"
    STALE = "stale"
    ACTION_REQUIRED = "action_required"


@dataclasses.dataclass
class Result:
    conclusion: Conclusion
    title: str
    summary: str
    annotations: typing.Optional[typing.List[str]] = None


async def get_checks_for_ref(
    ctxt: "context.Context",
    sha: github_types.SHAType,
    check_name: typing.Optional[str] = None,
) -> typing.List[github_types.GitHubCheckRun]:
    if check_name is None:
        kwargs = {}
    else:
        kwargs = {"check_name": check_name}
    checks: typing.List[github_types.GitHubCheckRun] = [
        check
        async for check in ctxt.client.items(
            f"{ctxt.base_url}/commits/{sha}/check-runs",
            list_items="check_runs",
            **kwargs,
        )
    ]
    return checks


def compare_dict(d1, d2, keys):
    for key in keys:
        if d1.get(key) != d2.get(key):
            return False
    return True


def check_need_update(
    previous_check: github_types.GitHubCheckRun,
    expected_check: GitHubCheckRunParameters,
) -> bool:
    if compare_dict(
        expected_check,
        previous_check,
        ("head_sha", "status", "conclusion", "details_url"),
    ):
        if previous_check["output"] == expected_check["output"]:
            return False
        elif previous_check["output"] is not None and compare_dict(
            expected_check["output"], previous_check["output"], ("title", "summary")
        ):
            return False

    return True


async def set_check_run(
    ctxt: "context.Context",
    name: str,
    result: Result,
    external_id: typing.Optional[str] = None,
) -> github_types.GitHubCheckRun:
    if result.conclusion is Conclusion.PENDING:
        status = Status.IN_PROGRESS
    else:
        status = Status.COMPLETED

    post_parameters = GitHubCheckRunParameters(
        {
            "name": name,
            "head_sha": ctxt.pull["head"]["sha"],
            "status": typing.cast(github_types.GitHubCheckRunStatus, status.value),
            "started_at": utils.utcnow().isoformat(),
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
    if summary and len(summary) > 65535:
        post_parameters["output"]["summary"] = utils.unicode_truncate(summary, 65532)
        post_parameters["output"]["summary"] += "…"  # this is 3 bytes long

    if external_id:
        post_parameters["external_id"] = external_id

    if status is Status.COMPLETED:
        post_parameters["conclusion"] = result.conclusion.value
        post_parameters["completed_at"] = utils.utcnow().isoformat()

    checks = sorted(
        (c for c in await ctxt.pull_engine_check_runs if c["name"] == name),
        key=lambda c: c["id"],
        reverse=True,
    )

    # Only keep the newer checks, cancelled others
    for check_to_cancelled in checks[1:]:
        if Status(check_to_cancelled["status"]) != Status.COMPLETED:
            await ctxt.client.patch(
                f"{ctxt.base_url}/check-runs/{check_to_cancelled['id']}",
                json={
                    "conclusion": Conclusion.CANCELLED.value,
                    "status": Status.COMPLETED.value,
                },
            )

    if not checks or (
        Status(checks[0]["status"]) == Status.COMPLETED and status == Status.IN_PROGRESS
    ):
        # NOTE(sileht): First time we see it, or the previous one have been completed and
        # now go back to in_progress. Since GitHub doesn't allow to change status of
        # completed check-runs, we have to create a new one.
        new_check = typing.cast(
            github_types.GitHubCheckRun,
            (
                await ctxt.client.post(
                    f"{ctxt.base_url}/check-runs",
                    json=post_parameters,
                )
            ).json(),
        )
    else:
        post_parameters["details_url"] += f"?check_run_id={checks[0]['id']}"

        # Don't do useless update
        if check_need_update(checks[0], post_parameters):
            new_check = typing.cast(
                github_types.GitHubCheckRun,
                (
                    await ctxt.client.patch(
                        f"{ctxt.base_url}/check-runs/{checks[0]['id']}",
                        json=post_parameters,
                    )
                ).json(),
            )
        else:
            new_check = checks[0]

    await ctxt.update_pull_check_runs(new_check)
    return new_check
