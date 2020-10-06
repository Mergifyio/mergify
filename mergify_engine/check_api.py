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

from mergify_engine import utils


# Used to track check run created by Mergify but for the user via the checks action
# e.g.: we want the engine to be retriggered if the state of this kind of checks changes.
USER_CREATED_CHECKS = "user-created-checkrun"


class Status(enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Conclusion(enum.Enum):
    PENDING = None
    CANCELLED = "cancelled"
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    ACTION_REQUIRED = "action_required"


@dataclasses.dataclass
class Result:
    conclusion: Conclusion
    title: str
    summary: str
    annotations: typing.Optional[typing.List[str]] = None


def get_checks_for_ref(ctxt, sha, **kwargs):
    checks = list(
        ctxt.client.items(
            f"{ctxt.base_url}/commits/{sha}/check-runs",
            api_version="antiope",
            list_items="check_runs",
            **kwargs,
        )
    )

    # FIXME(sileht): We currently have some issue to set back
    # conclusion to null, Maybe a GH bug or not.
    # As we rely heavily on conclusion to known if we have something to
    # evaluate or not, here a workaround:
    for check in checks:
        if check["status"] == "in_progress":
            check["conclusion"] = None

    return checks


def compare_dict(d1, d2, keys):
    for key in keys:
        if d1.get(key) != d2.get(key):
            return False
    return True


def set_check_run(ctxt, name, result, external_id=None):
    if result.conclusion is Conclusion.PENDING:
        status = Status.IN_PROGRESS
    else:
        status = Status.COMPLETED

    post_parameters = {
        "name": name,
        "head_sha": ctxt.pull["head"]["sha"],
        "status": status.value,
        "started_at": utils.utcnow().isoformat(),
        "details_url": f"{ctxt.pull['html_url']}/checks",
        "output": {
            "title": result.title,
            "summary": result.summary,
        },
    }

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

    checks = [c for c in ctxt.pull_engine_check_runs if c["name"] == name]

    if not checks:
        check = ctxt.client.post(
            f"{ctxt.base_url}/check-runs",
            api_version="antiope",
            json=post_parameters,
        ).json()
        ctxt.update_pull_check_runs(check)
        return check

    elif len(checks) > 1:
        ctxt.log.warning(
            "Multiple mergify checks have been created, we got the known race.",
        )

    post_parameters["details_url"] += "?check_run_id=%s" % checks[0]["id"]

    # FIXME(sileht): We have no (simple) way to ensure we don't have multiple
    # worker doing POST at the same time. It's unlike to happen, but it has
    # happen once, so to ensure Mergify continue to work, we update all
    # checks. User will see the check twice for a while, but it's better than
    # having Mergify stuck
    for check in checks:
        # Don't do useless update
        if compare_dict(
            post_parameters,
            check,
            ("name", "head_sha", "status", "conclusion", "details_url"),
        ):
            if check["output"] == post_parameters["output"]:
                continue
            elif check["output"] is not None and compare_dict(
                post_parameters["output"], check["output"], ("title", "summary")
            ):
                continue

        check = ctxt.client.patch(
            f"{ctxt.base_url}/check-runs/{check['id']}",
            api_version="antiope",
            json=post_parameters,
        ).json()

    ctxt.update_pull_check_runs(check)
    return check
