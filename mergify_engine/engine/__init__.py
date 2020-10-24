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

import daiquiri
import pkg_resources
import voluptuous
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.engine import actions_runner
from mergify_engine.engine import commands_runner


LOG = daiquiri.getLogger(__name__)

mergify_rule_path = pkg_resources.resource_filename(
    __name__, "../data/default_pull_request_rules.yml"
)

with open(mergify_rule_path, "r") as f:
    DEFAULT_PULL_REQUEST_RULES = voluptuous.Schema(rules.PullRequestRulesSchema)(
        yaml.safe_load(f.read())["rules"]
    )


def check_configuration_changes(ctxt):
    if ctxt.pull["base"]["repo"]["default_branch"] == ctxt.pull["base"]["ref"]:
        ref = None
        for f in ctxt.files:
            if f["filename"] in rules.MERGIFY_CONFIG_FILENAMES:
                ref = f["contents_url"].split("?ref=")[1]

        if ref is not None:
            try:
                rules.get_mergify_config(
                    ctxt.client, ctxt.pull["base"]["repo"]["name"], ref=ref
                )
            except rules.InvalidRules as e:
                # Not configured, post status check with the error message
                ctxt.set_summary_check(
                    check_api.Result(
                        check_api.Conclusion.FAILURE,
                        title="The new Mergify configuration is invalid",
                        summary=str(e),
                        annotations=e.get_annotations(e.filename),
                    )
                )
            else:
                ctxt.set_summary_check(
                    check_api.Result(
                        check_api.Conclusion.SUCCESS,
                        title="The new Mergify configuration is valid",
                        summary="This pull request must be merged manually because it modifies Mergify configuration",
                    )
                )

            return True
    return False


def get_summary_from_sha(ctxt, sha):
    checks = check_api.get_checks_for_ref(
        ctxt,
        sha,
        check_name=ctxt.SUMMARY_NAME,
    )
    checks = [c for c in checks if c["app"]["id"] == config.INTEGRATION_ID]
    if checks:
        return checks[0]


def ensure_summary_on_head_sha(ctxt):
    for check in ctxt.pull_engine_check_runs:
        if check["name"] == ctxt.SUMMARY_NAME:
            return

    sha = ctxt.get_cached_last_summary_head_sha()
    if sha:
        previous_summary = get_summary_from_sha(ctxt, sha)
    else:
        previous_summary = None
        ctxt.log.warning(
            "the pull request doesn't have the last summary head sha stored in redis"
        )

    if previous_summary:
        ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion(previous_summary["conclusion"]),
                title=previous_summary["output"]["title"],
                summary=previous_summary["output"]["summary"],
            )
        )
    else:
        ctxt.log.warning("the pull request doesn't have a summary")


def run(client, pull, sub, sources):
    LOG.debug("engine get context")
    ctxt = context.Context(client, pull, sub)
    ctxt.log.debug("engine start processing context")

    issue_comment_sources = []

    for source in sources:
        if source["event_type"] == "issue_comment":
            issue_comment_sources.append(source)
        else:
            ctxt.sources.append(source)

    ctxt.log.debug("engine run pending commands")
    commands_runner.run_pending_commands_tasks(ctxt)

    if issue_comment_sources:
        ctxt.log.debug("engine handle commands")
        for source in issue_comment_sources:
            commands_runner.handle(
                ctxt,
                source["data"]["comment"]["body"],
                source["data"]["comment"]["user"],
            )

    if not ctxt.sources:
        return

    if ctxt.client.auth.permissions_need_to_be_updated:
        ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Required GitHub permissions are missing.",
                summary="You can accept them at https://dashboard.mergify.io/",
            )
        )
        return

    ctxt.log.debug("engine check configuration change")
    if check_configuration_changes(ctxt):
        ctxt.log.info("Configuration changed, ignoring")
        return

    ctxt.log.debug("engine get configuration")
    # BRANCH CONFIGURATION CHECKING
    try:
        filename, mergify_config = rules.get_mergify_config(
            ctxt.client, ctxt.pull["base"]["repo"]["name"]
        )
    except rules.NoRules:  # pragma: no cover
        ctxt.log.info("No need to proceed queue (.mergify.yml is missing)")
        return
    except rules.InvalidRules as e:  # pragma: no cover
        ctxt.log.info(
            "The Mergify configuration is invalid",
            summary=str(e),
            annotations=e.get_annotations(e.filename),
        )
        # Not configured, post status check with the error message
        if any(
            (
                s["event_type"] == "pull_request"
                and s["data"]["action"] in ["opened", "synchronize"]
                for s in ctxt.sources
            )
        ):
            ctxt.set_summary_check(
                check_api.Result(
                    check_api.Conclusion.FAILURE,
                    title="The Mergify configuration is invalid",
                    summary=str(e),
                    annotations=e.get_annotations(e.filename),
                )
            )
        return

    # Add global and mandatory rules
    mergify_config["pull_request_rules"].rules.extend(DEFAULT_PULL_REQUEST_RULES.rules)

    if ctxt.pull["base"]["repo"]["private"] and not ctxt.subscription.has_feature(
        subscription.Features.PRIVATE_REPOSITORY
    ):
        ctxt.log.info("mergify disabled: private repository")
        ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Mergify is disabled",
                summary=sub.reason,
            )
        )
        return

    ensure_summary_on_head_sha(ctxt)

    # NOTE(jd): that's fine for now, but I wonder if we wouldn't need a higher abstraction
    # to have such things run properly. Like hooks based on events that you could
    # register. It feels hackish otherwise.
    if any(
        s["event_type"] == "pull_request" and s["data"]["action"] == "closed"
        for s in ctxt.sources
    ):
        ctxt.clear_cached_last_summary_head_sha()

    ctxt.log.debug("engine handle actions")
    actions_runner.handle(mergify_config["pull_request_rules"], ctxt)


async def create_initial_summary(owner, event_type, data):
    if event_type != "pull_request":
        return

    if data["action"] != "opened":
        return

    redis = await utils.get_aredis_for_cache()

    if not await redis.exists(
        rules.get_config_location_cache_key(
            owner, data["pull_request"]["base"]["repo"]["name"]
        )
    ):
        # Mergify is probably not activated on this repo
        return

    async with await github.aget_client(owner) as client:

        # NOTE(sileht): It's possible that a "push" event creates a summary before we
        # received the pull_request/opened event.
        # So we check first if a summary does not already exists, to not post
        # the summary twice. Since this method can ran in parallel of the worker
        # this is not a 100% reliable solution, but if we post a duplicate summary
        # check_api.set_check_run() handle this case and update both to not confuse users.
        sha = await redis.get(
            context.Context.redis_last_summary_head_sha_key(
                client, data["pull_request"]
            )
        )
        if sha:
            return

        post_parameters = {
            "name": context.Context.SUMMARY_NAME,
            "head_sha": data["pull_request"]["head"]["sha"],
            "status": check_api.Status.IN_PROGRESS.value,
            "started_at": utils.utcnow().isoformat(),
            "details_url": f"{data['pull_request']['html_url']}/checks",
            "output": {
                "title": "Your rules are under evaluation",
                "summary": "Be patient, the page will be updated soon.",
            },
        }
        await client.post(
            f"/repos/{data['pull_request']['base']['user']['login']}/{data['pull_request']['base']['repo']['name']}/check-runs",
            api_version="antiope",
            json=post_parameters,
        )
        await redis.set(
            context.Context.redis_last_summary_head_sha_key(
                client, data["pull_request"]
            ),
            data["pull_request"]["head"]["sha"],
            ex=context.SUMMARY_SHA_EXPIRATION,
        )
