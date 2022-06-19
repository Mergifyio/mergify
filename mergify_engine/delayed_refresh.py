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

import datetime
import typing

import daiquiri

from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine import worker
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers


if typing.TYPE_CHECKING:
    from mergify_engine import context
    from mergify_engine import rules

LOG = daiquiri.getLogger(__name__)

DELAYED_REFRESH_KEY = "delayed-refresh"


def _redis_key(
    repository: "context.Repository", pull_number: github_types.GitHubPullRequestNumber
) -> str:
    return f"{repository.installation.owner_id}~{repository.installation.owner_login}~{repository.repo['id']}~{repository.repo['name']}~{pull_number}"


async def _get_current_refresh_datetime(
    repository: "context.Repository",
    pull_number: github_types.GitHubPullRequestNumber,
) -> typing.Optional[datetime.datetime]:
    score = await repository.installation.redis.cache.zscore(
        DELAYED_REFRESH_KEY, _redis_key(repository, pull_number)
    )
    if score is not None:
        return date.fromtimestamp(float(score))
    return None


async def _set_current_refresh_datetime(
    repository: "context.Repository",
    pull_number: github_types.GitHubPullRequestNumber,
    at: datetime.datetime,
) -> None:
    await repository.installation.redis.cache.zadd(
        DELAYED_REFRESH_KEY,
        {_redis_key(repository, pull_number): at.timestamp()},
    )


async def plan_next_refresh(
    ctxt: "context.Context",
    _rules: typing.Union[
        typing.List["rules.EvaluatedRule"], typing.List["rules.EvaluatedQueueRule"]
    ],
    pull_request: "context.BasePullRequest",
) -> None:
    best_bet = await _get_current_refresh_datetime(ctxt.repository, ctxt.pull["number"])
    if best_bet is not None and best_bet < date.utcnow():
        best_bet = None

    for rule in _rules:
        f = filter.NearDatetimeFilter(rule.conditions.extract_raw_filter_tree())
        live_resolvers.configure_filter(ctxt.repository, f)
        try:
            bet = await f(pull_request)
        except live_resolvers.LiveResolutionFailure:
            continue
        if best_bet is None or best_bet > bet:
            best_bet = bet

    if best_bet is None or best_bet >= date.DT_MAX:
        zset_subkey = _redis_key(ctxt.repository, ctxt.pull["number"])
        removed = await ctxt.redis.cache.zrem(DELAYED_REFRESH_KEY, zset_subkey)
        if removed is not None and removed > 0:
            ctxt.log.info("unplan to refresh pull request")
    else:
        await _set_current_refresh_datetime(
            ctxt.repository, ctxt.pull["number"], best_bet
        )
        ctxt.log.info(
            "plan to refresh pull request", refresh_planned_at=best_bet.isoformat()
        )


async def plan_refresh_at_least_at(
    repository: "context.Repository",
    pull_number: github_types.GitHubPullRequestNumber,
    at: datetime.datetime,
) -> None:
    current = await _get_current_refresh_datetime(repository, pull_number)

    if current is not None and current < at:
        return

    await _set_current_refresh_datetime(repository, pull_number, at)
    repository.log.info(
        "override plan to refresh pull request", refresh_planned_at=at.isoformat()
    )


async def send(
    redis_links: redis_utils.RedisLinks,
) -> None:
    score = date.utcnow().timestamp()
    keys = await redis_links.cache.zrangebyscore(DELAYED_REFRESH_KEY, "-inf", score)
    if not keys:
        return

    pipe = await redis_links.stream.pipeline()
    keys_to_delete = set()
    for subkey in keys:
        (
            owner_id_str,
            owner_login_str,
            repository_id_str,
            repository_name_str,
            pull_request_number_str,
        ) = subkey.split("~")
        owner_id = github_types.GitHubAccountIdType(int(owner_id_str))
        repository_id = github_types.GitHubRepositoryIdType(int(repository_id_str))
        pull_request_number = github_types.GitHubPullRequestNumber(
            int(pull_request_number_str)
        )
        repository_name = github_types.GitHubRepositoryName(repository_name_str)
        owner_login = github_types.GitHubLogin(owner_login_str)

        LOG.info(
            "sending delayed pull request refresh",
            gh_owner=owner_login,
            gh_repo=repository_name,
            action="internal",
            source="delayed-refresh",
        )

        await worker.push(
            pipe,
            owner_id,
            owner_login,
            repository_id,
            repository_name,
            pull_request_number,
            "refresh",
            {
                "action": "internal",
                "ref": None,
                "source": "delayed-refresh",
            },  # type: ignore[typeddict-item]
            score=str(worker.get_priority_score(worker.Priority.medium)),
        )
        keys_to_delete.add(subkey)

    await pipe.execute()
    await redis_links.cache.zrem(DELAYED_REFRESH_KEY, *keys_to_delete)
