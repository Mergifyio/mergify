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

from mergify_engine import config
from mergify_engine import utils


def get_checks_for_ref(ctxt, sha, mergify_only=False, **kwargs):
    checks = ctxt.client.items(
        f"commits/{sha}/check-runs",
        api_version="antiope",
        list_items="check_runs",
        **kwargs,
    )

    if mergify_only:
        checks = [c for c in checks if c["app"]["id"] == config.INTEGRATION_ID]
    else:
        checks = list(checks)

    # FIXME(sileht): We currently have some issue to set back
    # conclusion to null, Maybe a GH bug or not.
    # As we rely heavily on conclusion to known if we have something to
    # evaluate or not, here a workaround:
    for check in checks:
        if check["status"] == "in_progress":
            check["conclusion"] = None

    return checks


def get_checks(ctxt, mergify_only=False, **kwargs):
    return get_checks_for_ref(ctxt, ctxt.pull["head"]["sha"], mergify_only, **kwargs)


def compare_dict(d1, d2, keys):
    for key in keys:
        if d1.get(key) != d2.get(key):
            return False
    return True


def set_check_run(ctxt, name, status, conclusion=None, output=None):
    post_parameters = {
        "name": name,
        "head_sha": ctxt.pull["head"]["sha"],
        "status": status,
    }
    if conclusion:
        post_parameters["conclusion"] = conclusion
    if output:
        # Maximum output/summary length for Check API is 65535
        summary = output.get("summary")
        if summary and len(summary) > 65535:
            output["summary"] = utils.unicode_truncate(summary, 65532)
            output["summary"] += "…"  # this is 3 bytes long
        post_parameters["output"] = output

    post_parameters["started_at"] = utils.utcnow().isoformat()
    post_parameters["details_url"] = "%s/checks" % ctxt.pull["html_url"]

    if status == "completed":
        post_parameters["completed_at"] = utils.utcnow().isoformat()

    checks = get_checks(ctxt, check_name=name, mergify_only=True)

    if not checks:
        checks = [
            ctxt.client.post(
                "check-runs", api_version="antiope", json=post_parameters,
            ).json()
        ]

    if len(checks) > 1:
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
            if check["output"] == output:
                continue
            elif (
                check["output"] is not None
                and output is not None
                and compare_dict(output, check["output"], ("title", "summary"))
            ):
                continue

        check = ctxt.client.patch(
            f"check-runs/{check['id']}", api_version="antiope", json=post_parameters,
        ).json()

    return check
