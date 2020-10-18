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

import contextlib
import dataclasses
import typing
import uuid

import daiquiri
from datadog import statsd

from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.clients import http
from mergify_engine.engine import commands_runner


LOG = daiquiri.getLogger(__name__)


def get_ignore_reason(event_type, data):
    if "repository" not in data:
        return "no repository found"

    elif event_type in ["installation", "installation_repositories"]:
        return f"action {data['action']}"

    elif event_type in ["push"] and not data["ref"].startswith("refs/heads/"):
        return f"push on {data['ref']}"

    elif event_type == "status" and data["state"] == "pending":
        return "state pending"

    elif event_type == "check_suite" and data["action"] != "rerequested":
        return f"check_suite/{data['action']}"

    elif (
        event_type in ["check_run", "check_suite"]
        and data[event_type]["app"]["id"] == config.INTEGRATION_ID
        and data["action"] != "rerequested"
        and data[event_type].get("external_id") != check_api.USER_CREATED_CHECKS
    ):
        return f"mergify {event_type}"

    elif event_type == "issue_comment" and data["action"] != "created":
        return f"comment has been {data['action']}"

    elif (
        event_type == "issue_comment"
        and "@mergify " not in data["comment"]["body"].lower()
        and "@mergifyio " not in data["comment"]["body"].lower()
    ):
        return "comment is not for Mergify"

    if data["repository"].get("archived"):  # pragma: no cover
        return "repository archived"

    elif event_type not in [
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "push",
        "status",
        "check_suite",
        "check_run",
        "refresh",
    ]:
        return "unexpected event_type"


def meter_event(event_type, data):
    tags = [f"event_type:{event_type}"]

    if "action" in data:
        tags.append(f"action:{data['action']}")

    if (
        event_type == "pull_request"
        and data["action"] == "closed"
        and data["pull_request"]["merged"]
    ):
        if data["pull_request"]["merged_by"] and data["pull_request"]["merged_by"][
            "login"
        ] in ["mergify[bot]", "mergify-test[bot]"]:
            tags.append("by_mergify")

    statsd.increment("github.events", tags=tags)


def _extract_source_data(event_type, data):
    slim_data = {"sender": data["sender"]}

    # To extract pull request numbers
    if event_type == "status":
        slim_data["sha"] = data["sha"]
    elif event_type in ("refresh", "push") and "ref" in data:
        slim_data["ref"] = data["ref"]
    elif event_type in ("check_suite", "check_run"):
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

    # For pull_request opened/synchronise/closed
    # and refresh event
    for attr in ("action", "after", "before"):
        if attr in data:
            slim_data[attr] = data[attr]

    # For commands runner
    if event_type == "issue_comment":
        slim_data["comment"] = data["comment"]
    return slim_data


@dataclasses.dataclass
class IgnoredEvent(Exception):
    """Raised when an is ignored."""

    event_type: str
    event_id: str
    reason: str


async def job_filter_and_dispatch(redis, event_type, event_id, data):
    # TODO(sileht): is statsd async ?
    meter_event(event_type, data)

    if "repository" in data:
        owner = data["repository"]["owner"]["login"]
        repo = data["repository"]["name"]
    else:
        owner = "<unknown>"
        repo = "<unknown>"

    reason = get_ignore_reason(event_type, data)
    if reason:
        msg_action = f"ignored: {reason}"
    else:
        msg_action = "pushed to worker"
        source_data = _extract_source_data(event_type, data)

        if "pull_request" in data:
            pull_number = data["pull_request"]["number"]
        elif event_type == "issue_comment":
            pull_number = data["issue"]["number"]
        else:
            pull_number = None

        await worker.push(
            redis,
            owner,
            repo,
            pull_number,
            event_type,
            source_data,
        )

        # NOTE(sileht): nothing important should happen in this hook as we don't retry it
        try:
            await commands_runner.on_each_event(owner, repo, event_type, data)
        except Exception as e:
            if exceptions.should_be_ignored(e) or exceptions.need_retry(e):
                LOG.debug("commands_runner.on_each_event failed", exc_info=True)
            else:
                raise

    LOG.info(
        "GithubApp event %s",
        msg_action,
        event_type=event_type,
        event_id=event_id,
        sender=data["sender"]["login"],
        gh_owner=owner,
        gh_repo=repo,
    )

    if reason:
        raise IgnoredEvent(event_type, event_id, reason)


SHA_EXPIRATION = 60


async def _get_github_pulls_from_sha(client, repo, sha):
    redis = await utils.get_aredis_for_cache()
    cache_key = f"sha~{client.auth.owner}~{repo}~{sha}"
    pull_number = await redis.get(cache_key)
    if pull_number is None:
        async for pull in client.items(f"/repos/{client.auth.owner}/{repo}/pulls"):
            if pull["head"]["sha"] == sha:
                await redis.set(cache_key, pull["number"], ex=SHA_EXPIRATION)
                return [pull["number"]]

        await redis.set(cache_key, -1, ex=SHA_EXPIRATION)
        return []
    elif pull_number == -1:
        return []
    else:
        return [int(pull_number)]


async def extract_pull_numbers_from_event(client, repo, event_type, data):
    # NOTE(sileht): Don't fail if we received even on repo that doesn't exists anymore
    with contextlib.suppress(http.HTTPNotFound):
        pulls_url = f"/repos/{client.auth.owner}/{repo}/pulls"
        if event_type == "refresh":
            if "ref" in data:
                branch = data["ref"][11:]  # refs/heads/
                return [p["number"] async for p in client.items(pulls_url, base=branch)]
            else:
                return [p["number"] async for p in client.items(pulls_url)]
        elif event_type == "push":
            branch = data["ref"][11:]  # refs/heads/
            return [p["number"] async for p in client.items(pulls_url, base=branch)]
        elif event_type == "status":
            return await _get_github_pulls_from_sha(client, repo, data["sha"])
        elif event_type in ["check_suite", "check_run"]:
            # NOTE(sileht): This list may contains Pull Request from another org/user fork...
            base_repo_url = f"{config.GITHUB_API_URL}/repos/{client.auth.owner}/{repo}"
            pulls = data[event_type]["pull_requests"]
            # TODO(sileht): remove `"base" in p and`
            # Due to MERGIFY-ENGINE-1JZ, we have to temporary ignore pull with base missing
            pulls = [
                p["number"]
                for p in pulls
                if "base" in p and p["base"]["repo"]["url"] == base_repo_url
            ]
            if not pulls:
                sha = data[event_type]["head_sha"]
                pulls = await _get_github_pulls_from_sha(client, repo, sha)
            return pulls
    return []


# TODO(sileht): use Enum for action
async def send_refresh(pull: typing.Dict, action: str = "user"):
    data = {
        "action": action,
        "repository": pull["base"]["repo"],
        "pull_request": pull,
        "sender": {"login": "<internal>"},
    }
    redis = await utils.create_aredis_for_stream()
    try:
        await job_filter_and_dispatch(redis, "refresh", str(uuid.uuid4()), data)
    finally:
        redis.connection_pool.disconnect()
