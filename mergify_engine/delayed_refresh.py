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

from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine import worker
from mergify_engine.rules import filter
from mergify_engine.rules import live_resolvers


LOG = daiquiri.getLogger(__name__)

DELAYED_REFRESH_KEY = "delayed-refresh"


async def plan_next_refresh(
    ctxt: context.Context,
    _rules: typing.Union[
        typing.List[rules.EvaluatedRule], typing.List[rules.EvaluatedQueueRule]
    ],
    pull_request: context.BasePullRequest,
) -> None:
    zset_subkey = f"{ctxt.repository.installation.owner_id}~{ctxt.repository.installation.owner_login}~{ctxt.repository.repo['id']}~{ctxt.repository.repo['name']}~{ctxt.pull['number']}"
    best_bet: typing.Optional[datetime.datetime] = None

    score = await ctxt.redis.zscore(DELAYED_REFRESH_KEY, zset_subkey)
    if score is not None:
        best_bet = date.fromtimestamp(float(score))
        if best_bet < date.utcnow():
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
        removed = await ctxt.redis.zrem(DELAYED_REFRESH_KEY, zset_subkey)
        if removed is not None and removed > 0:
            ctxt.log.info("unplan to refresh pull request")
    else:
        await ctxt.redis.zadd(
            DELAYED_REFRESH_KEY,
            **{zset_subkey: best_bet.timestamp()},
        )
        ctxt.log.info(
            "plan to refresh pull request", refresh_planned_at=best_bet.isoformat()
        )


async def send(
    redis_stream: utils.RedisStream,
    redis_cache: utils.RedisCache,
) -> None:
    score = date.utcnow().timestamp()
    keys = await redis_cache.zrangebyscore(DELAYED_REFRESH_KEY, "-inf", score)
    if not keys:
        return

    pipe = await redis_stream.pipeline()
    keys_to_delete = set()
    for subkey in keys:
        (
            owner_id_str,
            owner_login,
            repository_id_str,
            repository_name,
            pull_request_number_str,
        ) = subkey.split("~")
        owner_id = github_types.GitHubAccountIdType(int(owner_id_str))
        repository_id = github_types.GitHubRepositoryIdType(int(repository_id_str))
        pull_request_number = github_types.GitHubPullRequestNumber(
            int(pull_request_number_str)
        )

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
        )
        keys_to_delete.add(subkey)

    await pipe.execute()
    await redis_cache.zrem(DELAYED_REFRESH_KEY, *keys_to_delete)
