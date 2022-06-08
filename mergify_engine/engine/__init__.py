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
import typing

import daiquiri
import first

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine import rules
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.engine import actions_runner
from mergify_engine.engine import commands_runner
from mergify_engine.engine import queue_runner


LOG = daiquiri.getLogger(__name__)


@dataclasses.dataclass
class MultipleConfigurationFileFound(Exception):
    files: typing.List[context.MergifyConfigFile]


async def _check_configuration_changes(
    ctxt: context.Context,
    current_mergify_config_file: typing.Optional[context.MergifyConfigFile],
) -> bool:
    if ctxt.pull["base"]["repo"]["default_branch"] != ctxt.pull["base"]["ref"]:
        return False

    if ctxt.closed:
        # merge_commit_sha is a merge between the PR and the base branch only when the pull request is open
        # after it's None or the resulting commit of the pull request merge (maybe a rebase, squash, merge).
        # As the PR is closed, we don't care about the config change detector.
        return False

    # NOTE(sileht): This heuristic works only if _ensure_summary_on_head_sha()
    # is called after _check_configuration_changes().
    # If we don't have the real summary yet it means the pull request has just
    # been open or synchronize or we never see it.
    summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
    if summary and summary["output"]["title"] not in (
        constants.INITIAL_SUMMARY_TITLE,
        constants.CONFIGURATION_MUTIPLE_FOUND_SUMMARY_TITLE,
    ):
        if await ctxt.get_engine_check_run(constants.CONFIGURATION_CHANGED_CHECK_NAME):
            return True
        elif await ctxt.get_engine_check_run(
            constants.CONFIGURATION_DELETED_CHECK_NAME
        ):
            return True
        else:
            return False

    preferred_filename = (
        None
        if current_mergify_config_file is None
        else current_mergify_config_file["path"]
    )

    # NOTE(sileht): pull.base.sha is unreliable as its the sha when the PR is
    # open and not the merge-base/fork-point. So we compare the configuration from the base
    # branch with the one of the merge commit. If the configuration is changed by the PR, they will be
    # different.
    config_files: typing.Dict[str, context.MergifyConfigFile] = {}
    async for config_file in ctxt.repository.iter_mergify_config_files(
        ref=ctxt.pull["merge_commit_sha"], preferred_filename=preferred_filename
    ):
        config_files[config_file["path"]] = config_file

    if len(config_files) >= 2:
        raise MultipleConfigurationFileFound(list(config_files.values()))

    future_mergify_config_file = (
        list(config_files.values())[0] if config_files else None
    )

    if current_mergify_config_file is None:
        if future_mergify_config_file is None:
            return False
    else:
        if future_mergify_config_file is None:
            # Configuration is deleted by the pull request
            await check_api.set_check_run(
                ctxt,
                constants.CONFIGURATION_DELETED_CHECK_NAME,
                check_api.Result(
                    check_api.Conclusion.SUCCESS,
                    title="The Mergify configuration has been deleted",
                    summary="Mergify will still continue to listen to commands.",
                ),
            )
            return True

        elif (
            current_mergify_config_file["path"] == future_mergify_config_file["path"]
            and current_mergify_config_file["sha"] == future_mergify_config_file["sha"]
        ):
            # Nothing change between main branch and the pull request
            return False

    try:
        rules.get_mergify_config(future_mergify_config_file)
    except rules.InvalidRules as e:
        # Not configured, post status check with the error message
        await check_api.set_check_run(
            ctxt,
            constants.CONFIGURATION_CHANGED_CHECK_NAME,
            check_api.Result(
                check_api.Conclusion.FAILURE,
                title="The new Mergify configuration is invalid",
                summary=str(e),
                annotations=e.get_annotations(e.filename),
            ),
        )
    else:
        await check_api.set_check_run(
            ctxt,
            constants.CONFIGURATION_CHANGED_CHECK_NAME,
            check_api.Result(
                check_api.Conclusion.SUCCESS,
                title="The new Mergify configuration is valid",
                summary="This pull request has to be merged manually",
            ),
        )

    return True


async def _get_summary_from_sha(
    ctxt: context.Context, sha: github_types.SHAType
) -> typing.Optional[github_types.CachedGitHubCheckRun]:
    return first.first(
        await check_api.get_checks_for_ref(
            ctxt,
            sha,
            check_name=constants.SUMMARY_NAME,
        ),
        key=lambda c: c["app_id"] == config.INTEGRATION_ID,
    )


async def _get_summary_from_synchronize_event(
    ctxt: context.Context,
) -> typing.Optional[github_types.CachedGitHubCheckRun]:
    synchronize_events = {
        typing.cast(github_types.GitHubEventPullRequest, s["data"])[
            "after"
        ]: typing.cast(github_types.GitHubEventPullRequest, s["data"])
        for s in ctxt.sources
        if s["event_type"] == "pull_request"
        and typing.cast(github_types.GitHubEventPullRequest, s["data"])["action"]
        == "synchronize"
        and "after" in s["data"]
    }
    if synchronize_events:
        ctxt.log.debug("checking summary from synchronize events")

        # NOTE(sileht): We sometimes got multiple synchronize events in a row, that's not
        # always the last one that has the Summary, so we also look in older ones if
        # necessary.
        after_sha = ctxt.pull["head"]["sha"]
        while synchronize_events:
            sync_event = synchronize_events.pop(after_sha, None)
            if sync_event:
                previous_summary = await _get_summary_from_sha(
                    ctxt, sync_event["before"]
                )
                if previous_summary and actions_runner.load_conclusions_line(
                    ctxt, previous_summary
                ):
                    ctxt.log.debug("got summary from synchronize events")
                    return previous_summary

                after_sha = sync_event["before"]
            else:
                break
    return None


async def _ensure_summary_on_head_sha(ctxt: context.Context) -> None:
    if ctxt.has_been_opened():
        return

    sha = await ctxt.get_cached_last_summary_head_sha()
    if sha is not None:
        if sha == ctxt.pull["head"]["sha"]:
            ctxt.log.debug("head sha didn't changed, no need to copy summary")
            return
        else:
            ctxt.log.debug(
                "head sha changed need to copy summary", gh_pull_previous_head_sha=sha
            )

    previous_summary = None

    if sha is not None:
        ctxt.log.debug("checking summary from redis")
        previous_summary = await _get_summary_from_sha(ctxt, sha)
        if previous_summary is not None:
            ctxt.log.debug("got summary from redis")

    if previous_summary is None:
        # NOTE(sileht): If the cached summary sha expires and the next event we got for
        # a pull request is "synchronize" we will lose the summary. Most of the times
        # it's not a big deal, but if the pull request is queued for merge, it may
        # be stuck.
        previous_summary = await _get_summary_from_synchronize_event(ctxt)

    # Sync only if the external_id is the expected one
    if previous_summary and (
        previous_summary["external_id"] is None
        or previous_summary["external_id"] == ""
        or previous_summary["external_id"] == str(ctxt.pull["number"])
    ):
        await ctxt.set_summary_check(
            check_api.Result(
                check_api.Conclusion(previous_summary["conclusion"]),
                title=previous_summary["output"]["title"],
                summary=previous_summary["output"]["summary"],
            )
        )
    elif previous_summary:
        ctxt.log.info(
            "got a previous summary, but collision detected with another pull request",
            other_pull=previous_summary["external_id"],
        )


class T_PayloadEventIssueCommentSource(typing.TypedDict):
    event_type: github_types.GitHubEventType
    data: github_types.GitHubEventIssueComment
    timestamp: str


async def run(
    ctxt: context.Context,
    sources: typing.List[context.T_PayloadEventSource],
) -> typing.Optional[check_api.Result]:
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

    permissions_need_to_be_updated = github_app.permissions_need_to_be_updated(
        ctxt.repository.installation.installation
    )
    if permissions_need_to_be_updated:
        return check_api.Result(
            check_api.Conclusion.FAILURE,
            title="Required GitHub permissions are missing.",
            summary="You can accept them at https://dashboard.mergify.com/",
        )

    if ctxt.pull["base"]["repo"]["private"]:
        if not ctxt.subscription.has_feature(subscription.Features.PRIVATE_REPOSITORY):
            ctxt.log.info(
                "mergify disabled: private repository", reason=ctxt.subscription.reason
            )
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Mergify is disabled",
                summary=ctxt.subscription.reason,
            )
    else:
        if not ctxt.subscription.has_feature(subscription.Features.PUBLIC_REPOSITORY):
            ctxt.log.info(
                "mergify disabled: public repository", reason=ctxt.subscription.reason
            )
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                title="Mergify is disabled",
                summary=ctxt.subscription.reason,
            )

    config_file = await ctxt.repository.get_mergify_config_file()

    try:
        ctxt.configuration_changed = await _check_configuration_changes(
            ctxt, config_file
        )
    except MultipleConfigurationFileFound as e:
        files = "\n * " + "\n * ".join(f["path"] for f in e.files)
        # NOTE(sileht): This replaces the summary, so we will may lost the
        # state of queue/comment action. But since we can't choice which config
        # file we need to use... we can't do much.
        return check_api.Result(
            check_api.Conclusion.FAILURE,
            title=constants.CONFIGURATION_MUTIPLE_FOUND_SUMMARY_TITLE,
            summary=f"You must keep only one of these configuration files in the repository: {files}",
        )

    # BRANCH CONFIGURATION CHECKING
    try:
        mergify_config = await ctxt.repository.get_mergify_config()
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
                    return check_api.Result(
                        check_api.Conclusion.FAILURE,
                        title="The current Mergify configuration is invalid",
                        summary=str(e),
                        annotations=e.get_annotations(e.filename),
                    )
        return None

    ctxt.log.debug("engine run pending commands")
    await commands_runner.run_pending_commands_tasks(ctxt, mergify_config)

    if issue_comment_sources:
        ctxt.log.debug("engine handle commands")
        for ic_source in issue_comment_sources:
            await commands_runner.handle(
                ctxt,
                mergify_config,
                ic_source["data"]["comment"]["body"],
                ic_source["data"]["comment"]["user"],
            )

    await _ensure_summary_on_head_sha(ctxt)

    summary = await ctxt.get_engine_check_run(constants.SUMMARY_NAME)
    if (
        summary
        and summary["external_id"] is not None
        and summary["external_id"] != ""
        and summary["external_id"] != str(ctxt.pull["number"])
    ):
        other_ctxt = await ctxt.repository.get_pull_request_context(
            github_types.GitHubPullRequestNumber(int(summary["external_id"]))
        )
        # NOTE(sileht): allow to override the summary of another pull request
        # only if this one is closed, but this can still confuse users as the
        # check-runs created by merge/queue action will not be cleaned.
        # TODO(sileht): maybe cancel all other mergify engine check-runs in this case?
        if not other_ctxt.closed:
            # TODO(sileht): try to report that without check-runs/statuses to the user
            # and without spamming him with comment
            ctxt.log.info(
                "sha collision detected between pull requests",
                other_pull=summary["external_id"],
            )
            return None

    if not ctxt.has_been_opened() and summary is None:
        ctxt.log.warning(
            "the pull request doesn't have a summary",
            head_sha=ctxt.pull["head"]["sha"],
        )

    ctxt.log.debug("engine handle actions")
    if ctxt.is_merge_queue_pr():
        return await queue_runner.handle(mergify_config["queue_rules"], ctxt)
    else:
        return await actions_runner.handle(mergify_config["pull_request_rules"], ctxt)


async def create_initial_summary(
    redis: redis_utils.RedisCache, event: github_types.GitHubEventPullRequest
) -> None:
    owner = event["repository"]["owner"]
    repo = event["pull_request"]["base"]["repo"]

    if not await redis.exists(
        context.Repository.get_config_file_cache_key(
            repo["id"],
        )
    ):
        # Mergify is probably not activated on this repo
        return

    # NOTE(sileht): It's possible that a "push" event creates a summary before we
    # received the pull_request/opened event.
    # So we check first if a summary does not already exists, to not post
    # the summary twice. Since this method can ran in parallel of the worker
    # this is not a 100% reliable solution, but if we post a duplicate summary
    # check_api.set_check_run() handle this case and update both to not confuse users.
    summary_exists = await context.Context.summary_exists(
        redis, owner["id"], repo["id"], event["pull_request"]
    )

    if summary_exists:
        return

    installation_json = await github.get_installation_from_account_id(owner["id"])
    async with github.aget_client(installation_json) as client:
        post_parameters = {
            "name": constants.SUMMARY_NAME,
            "head_sha": event["pull_request"]["head"]["sha"],
            "status": check_api.Status.IN_PROGRESS.value,
            "started_at": date.utcnow().isoformat(),
            "details_url": f"{event['pull_request']['html_url']}/checks",
            "output": {
                "title": constants.INITIAL_SUMMARY_TITLE,
                "summary": "Be patient, the page will be updated soon.",
            },
            "external_id": str(event["pull_request"]["number"]),
        }
        try:
            await client.post(
                f"/repos/{event['pull_request']['base']['user']['login']}/{event['pull_request']['base']['repo']['name']}/check-runs",
                api_version="antiope",
                json=post_parameters,
            )
        except http.HTTPClientSideError as e:
            if e.status_code == 422 and "No commit found for SHA" in e.message:
                return
            raise
