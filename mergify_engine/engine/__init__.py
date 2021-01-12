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
import typing

import daiquiri
import pkg_resources
import voluptuous
import yaml

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
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


async def _check_configuration_changes(ctxt: context.Context) -> bool:
    if ctxt.pull["base"]["repo"]["default_branch"] == ctxt.pull["base"]["ref"]:
        ref = None
        for f in ctxt.files:
            if f["filename"] in rules.MERGIFY_CONFIG_FILENAMES:
                ref = f["contents_url"].split("?ref=")[1]

        if ref is not None:
            try:
                await rules.get_mergify_config(
                    ctxt.client, ctxt.pull["base"]["repo"], ref=ref
                )
            except rules.InvalidRules as e:
                # Not configured, post status check with the error message
                await ctxt.set_summary_check(
                    check_api.Result(
                        check_api.Conclusion.FAILURE,
                        title="The new Mergify configuration is invalid",
                        summary=str(e),
                        annotations=e.get_annotations(e.filename),
                    )
                )
            else:
                await ctxt.set_summary_check(
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


async def _ensure_summary_on_head_sha(ctxt: context.Context) -> None:
    for check in ctxt.pull_engine_check_runs:
        if check["name"] == ctxt.SUMMARY_NAME and actions_runner.load_conclusions_line(
            check
        ):
            return

    sha = await ctxt.get_cached_last_summary_head_sha()
    if sha is None:
        previous_summary = None
        ctxt.log.warning(
            "the pull request doesn't have the last summary head sha stored in redis"
        )
    else:
        previous_summary = get_summary_from_sha(ctxt, sha)

    if previous_summary:
        await ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion(previous_summary["conclusion"]),
                title=previous_summary["output"]["title"],
                summary=previous_summary["output"]["summary"],
            )
        )
    else:
        ctxt.log.warning("the pull request doesn't have a summary")


class T_PayloadEventIssueCommentSource(typing.TypedDict):
    event_type: github_types.GitHubEventType
    data: github_types.GitHubEventIssueComment
    timestamp: str


async def run(
    ctxt: context.Context,
    sources: typing.List[context.T_PayloadEventSource],
) -> None:
    LOG.debug("engine get context")
    ctxt.log.debug("engine start processing context")

    issue_comment_sources: typing.List[T_PayloadEventIssueCommentSource] = []

    for source in sources:
        if source["event_type"] == "issue_comment":
            issue_comment_sources.append(
                typing.cast(T_PayloadEventIssueCommentSource, source)
            )
        else:
            ctxt.sources.append(source)

    ctxt.log.debug("engine run pending commands")
    await commands_runner.run_pending_commands_tasks(ctxt)

    if issue_comment_sources:
        ctxt.log.debug("engine handle commands")
        for ic_source in issue_comment_sources:
            await commands_runner.handle(
                ctxt,
                ic_source["data"]["comment"]["body"],
                ic_source["data"]["comment"]["user"],
            )

    if not ctxt.sources:
        return

    if ctxt.client.auth.permissions_need_to_be_updated:
        await ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Required GitHub permissions are missing.",
                summary="You can accept them at https://dashboard.mergify.io/",
            )
        )
        return

    ctxt.log.debug("engine check configuration change")
    if await _check_configuration_changes(ctxt):
        ctxt.log.info("Configuration changed, ignoring")
        return

    ctxt.log.debug("engine get configuration")
    # BRANCH CONFIGURATION CHECKING
    try:
        filename, mergify_config = await rules.get_mergify_config(
            ctxt.client, ctxt.pull["base"]["repo"]
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
        for s in ctxt.sources:
            if s["event_type"] == "pull_request":
                event = typing.cast(github_types.GitHubEventPullRequest, s["data"])
                if event["action"] in ("opened", "synchronize"):
                    await ctxt.set_summary_check(
                        check_api.Result(
                            check_api.Conclusion.FAILURE,
                            title="The Mergify configuration is invalid",
                            summary=str(e),
                            annotations=e.get_annotations(e.filename),
                        )
                    )
                    break
        return

    # Add global and mandatory rules
    mergify_config["pull_request_rules"].rules.extend(DEFAULT_PULL_REQUEST_RULES.rules)

    if ctxt.pull["base"]["repo"]["private"] and not ctxt.subscription.has_feature(
        subscription.Features.PRIVATE_REPOSITORY
    ):
        ctxt.log.info("mergify disabled: private repository")
        await ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Mergify is disabled",
                summary=ctxt.subscription.reason,
            )
        )
        return

    await _ensure_summary_on_head_sha(ctxt)

    # NOTE(jd): that's fine for now, but I wonder if we wouldn't need a higher abstraction
    # to have such things run properly. Like hooks based on events that you could
    # register. It feels hackish otherwise.
    for s in ctxt.sources:
        if s["event_type"] == "pull_request":
            event = typing.cast(github_types.GitHubEventPullRequest, s["data"])
            if event["action"] == "closed":
                ctxt.clear_cached_last_summary_head_sha()
                break

    ctxt.log.debug("engine handle actions")
    await actions_runner.handle(mergify_config["pull_request_rules"], ctxt)


async def create_initial_summary(event: github_types.GitHubEventPullRequest) -> None:
    owner = event["repository"]["owner"]["login"]

    async with utils.aredis_for_cache() as redis:
        if not await redis.exists(
            rules.get_config_location_cache_key(event["pull_request"]["base"]["repo"])
        ):
            # Mergify is probably not activated on this repo
            return

        # NOTE(sileht): It's possible that a "push" event creates a summary before we
        # received the pull_request/opened event.
        # So we check first if a summary does not already exists, to not post
        # the summary twice. Since this method can ran in parallel of the worker
        # this is not a 100% reliable solution, but if we post a duplicate summary
        # check_api.set_check_run() handle this case and update both to not confuse users.
        sha = await context.Context.get_cached_last_summary_head_sha_from_pull(
            redis, event["pull_request"]
        )

    if sha is not None or sha == event["pull_request"]["head"]["sha"]:
        return

    async with await github.aget_client(owner) as client:
        post_parameters = {
            "name": context.Context.SUMMARY_NAME,
            "head_sha": event["pull_request"]["head"]["sha"],
            "status": check_api.Status.IN_PROGRESS.value,
            "started_at": utils.utcnow().isoformat(),
            "details_url": f"{event['pull_request']['html_url']}/checks",
            "output": {
                "title": "Your rules are under evaluation",
                "summary": "Be patient, the page will be updated soon.",
            },
        }
        await client.post(
            f"/repos/{event['pull_request']['base']['user']['login']}/{event['pull_request']['base']['repo']['name']}/check-runs",
            api_version="antiope",  # type: ignore[call-arg]
            json=post_parameters,
        )
