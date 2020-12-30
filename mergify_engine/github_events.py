# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import uuid

import aredis
import daiquiri
from datadog import statsd

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import engine
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.engine import commands_runner


LOG = daiquiri.getLogger(__name__)


def meter_event(
    event_type: github_types.GitHubEventType, event: github_types.GitHubEvent
) -> None:
    tags = [f"event_type:{event_type}"]

    if event_type == "pull_request":
        event = typing.cast(github_types.GitHubEventPullRequest, event)
        tags.append(f"action:{event['action']}")
        if event["action"] == "closed" and event["pull_request"]["merged"]:
            if event["pull_request"]["merged_by"] is not None and event["pull_request"][
                "merged_by"
            ]["login"] in ["mergify[bot]", "mergify-test[bot]"]:
                tags.append("by_mergify")

    statsd.increment("github.events", tags=tags)


def _extract_slim_event(event_type, data):
    slim_data = {"sender": data["sender"]}

    if event_type == "status":
        # To get PR from sha
        slim_data["sha"] = data["sha"]

    elif event_type == "refresh":
        # To get PR from sha or branch name
        slim_data["action"] = data["action"]
        slim_data["ref"] = data["ref"]

    elif event_type == "push":
        # To get PR from sha
        slim_data["ref"] = data["ref"]

    elif event_type in ("check_suite", "check_run"):
        # To get PR from sha
        slim_data[event_type] = {
            "head_sha": data[event_type]["head_sha"],
            "pull_requests": [
                {
                    "number": p["number"],
                    "base": {"repo": {"url": p["base"]["repo"]["url"]}},
                }
                for p in data[event_type]["pull_requests"]
            ],
        }

    elif event_type == "pull_request":
        # For pull_request opened/synchronise/closed
        slim_data["action"] = data["action"]

    elif event_type == "issue_comment":
        # For commands runner
        slim_data["comment"] = data["comment"]

    return slim_data


@dataclasses.dataclass
class IgnoredEvent(Exception):
    """Raised when an is ignored."""

    event_type: str
    event_id: str
    reason: str


def _log_on_exception(exc: Exception, msg: str) -> None:
    if exceptions.should_be_ignored(exc) or exceptions.need_retry(exc):
        log = LOG.debug
    else:
        log = LOG.error
    log(msg, exc_info=exc)


async def filter_and_dispatch(
    redis: aredis.StrictRedis,
    event_type: github_types.GitHubEventType,
    event_id: str,
    event: github_types.GitHubEvent,
) -> None:
    # TODO(sileht): is statsd async ?
    meter_event(event_type, event)

    pull_number = None
    owner = "<unknown>"
    repo = "unknown"
    ignore_reason = None

    if event_type == "pull_request":
        event = typing.cast(github_types.GitHubEventPullRequest, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        pull_number = event["pull_request"]["number"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

        elif event["action"] == "opened":
            try:
                await engine.create_initial_summary(event)
            except Exception as e:
                _log_on_exception(e, "fail to create initial summary")

    elif event_type == "refresh":
        event = typing.cast(github_types.GitHubEventRefresh, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]

        if event["pull_request"] is not None:
            pull_number = event["pull_request"]["number"]

    elif event_type == "pull_request_review_comment":
        event = typing.cast(github_types.GitHubEventPullRequestReviewComment, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        pull_number = event["pull_request"]["number"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

    elif event_type == "pull_request_review":
        event = typing.cast(github_types.GitHubEventPullRequestReview, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        pull_number = event["pull_request"]["number"]
    elif event_type == "issue_comment":
        event = typing.cast(github_types.GitHubEventIssueComment, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        pull_number = event["issue"]["number"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

        elif event["action"] != "created":
            ignore_reason = f"comment has been {event['action']}"

        elif (
            "@mergify " not in event["comment"]["body"].lower()
            and "@mergifyio " not in event["comment"]["body"].lower()
        ):
            ignore_reason = "comment is not for Mergify"
        else:
            # NOTE(sileht): nothing important should happen in this hook as we don't retry it
            try:
                await commands_runner.on_each_event(event)
            except Exception as e:
                _log_on_exception(e, "commands_runner.on_each_event failed")

    elif event_type == "status":
        event = typing.cast(github_types.GitHubEventStatus, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

    elif event_type == "push":
        event = typing.cast(github_types.GitHubEventPush, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

        elif not event["ref"].startswith("refs/heads/"):
            ignore_reason = f"push on {event['ref']}"

        elif event["repository"]["archived"]:  # pragma: no cover
            ignore_reason = "repository archived"

    elif event_type == "check_suite":
        event = typing.cast(github_types.GitHubEventCheckSuite, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

        elif event["action"] != "rerequested":
            ignore_reason = f"check_suite/{event['action']}"

        elif (
            event[event_type]["app"]["id"] == config.INTEGRATION_ID
            and event["action"] != "rerequested"
            and event[event_type].get("external_id") != check_api.USER_CREATED_CHECKS
        ):
            ignore_reason = f"mergify {event_type}"

    elif event_type == "check_run":
        event = typing.cast(github_types.GitHubEventCheckRun, event)
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]

        if event["repository"]["archived"]:
            ignore_reason = "repository archived"

        elif (
            event[event_type]["app"]["id"] == config.INTEGRATION_ID
            and event["action"] != "rerequested"
            and event[event_type].get("external_id") != check_api.USER_CREATED_CHECKS
        ):
            ignore_reason = f"mergify {event_type}"
    else:
        ignore_reason = "unexpected event_type"

    if ignore_reason is None:
        msg_action = "pushed to worker"
        slim_event = _extract_slim_event(event_type, event)

        await worker.push(
            redis,
            owner,
            repo,
            pull_number,
            event_type,
            slim_event,
        )
    else:
        msg_action = f"ignored: {ignore_reason}"

    LOG.info(
        "GithubApp event %s",
        msg_action,
        event_type=event_type,
        event_id=event_id,
        sender=event["sender"]["login"],
        gh_owner=owner,
        gh_repo=repo,
    )

    if ignore_reason:
        raise IgnoredEvent(event_type, event_id, ignore_reason)


SHA_EXPIRATION = 60


async def _get_github_pulls_from_sha(client, repo, sha, pulls):
    redis = await utils.get_aredis_for_cache()
    cache_key = f"sha~{client.auth.owner}~{repo}~{sha}"
    pull_number = await redis.get(cache_key)
    if pull_number is None:
        for pull in pulls:
            if pull["head"]["sha"] == sha:
                await redis.set(cache_key, pull["number"], ex=SHA_EXPIRATION)
                return [pull["number"]]

        await redis.set(cache_key, -1, ex=SHA_EXPIRATION)
        return []
    elif pull_number == -1:
        return []
    else:
        return [int(pull_number)]


async def extract_pull_numbers_from_event(
    client: github.AsyncGithubInstallationClient,
    repo: str,
    event_type: github_types.GitHubEventType,
    data: github_types.GitHubEvent,
    opened_pulls: typing.List[github_types.GitHubPullRequest],
) -> typing.List[int]:
    # NOTE(sileht): Don't fail if we received even on repo that doesn't exists anymore
    if event_type == "refresh":
        data = typing.cast(github_types.GitHubEventRefresh, data)
        if (ref := data.get("ref")) is None:
            return [p["number"] for p in opened_pulls]
        else:
            branch = ref[11:]  # refs/heads/
            return [p["number"] for p in opened_pulls if p["base"]["ref"] == branch]
    elif event_type == "push":
        data = typing.cast(github_types.GitHubEventPush, data)
        branch = data["ref"][11:]  # refs/heads/
        return [p["number"] for p in opened_pulls if p["base"]["ref"] == branch]
    elif event_type == "status":
        data = typing.cast(github_types.GitHubEventStatus, data)
        return await _get_github_pulls_from_sha(client, repo, data["sha"], opened_pulls)
    elif event_type == "check_suite":
        data = typing.cast(github_types.GitHubEventCheckSuite, data)
        # NOTE(sileht): This list may contains Pull Request from another org/user fork...
        base_repo_url = f"{config.GITHUB_API_URL}/repos/{client.auth.owner}/{repo}"
        pulls = [
            p["number"]
            for p in data[event_type]["pull_requests"]
            if p["base"]["repo"]["url"] == base_repo_url
        ]
        if not pulls:
            sha = data[event_type]["head_sha"]
            pulls = await _get_github_pulls_from_sha(client, repo, sha, opened_pulls)
        return pulls
    elif event_type == "check_run":
        data = typing.cast(github_types.GitHubEventCheckRun, data)
        # NOTE(sileht): This list may contains Pull Request from another org/user fork...
        base_repo_url = f"{config.GITHUB_API_URL}/repos/{client.auth.owner}/{repo}"
        pulls = [
            p["number"]
            for p in data[event_type]["pull_requests"]
            if p["base"]["repo"]["url"] == base_repo_url
        ]
        if not pulls:
            sha = data[event_type]["head_sha"]
            pulls = await _get_github_pulls_from_sha(client, repo, sha, opened_pulls)
        return pulls
    else:
        return []


# TODO(sileht): use Enum for action
async def send_refresh(
    pull: github_types.GitHubPullRequest,
    action: github_types.GitHubEventRefreshActionType = "user",
) -> None:
    data = github_types.GitHubEventRefresh(
        {
            "action": action,
            "ref": None,
            "repository": pull["base"]["repo"],
            "pull_request": pull,
            "ref": None,
            "sender": {"login": "<internal>", "id": 0, "type": "User"},
            "organization": pull["base"]["repo"]["owner"],
            "installation": {
                "id": 0,
                "account": {"login": "", "id": 0, "type": "User"},
            },
        }
    )
    redis = await utils.create_aredis_for_stream()
    try:
        await filter_and_dispatch(redis, "refresh", str(uuid.uuid4()), data)
    finally:
        redis.connection_pool.disconnect()
