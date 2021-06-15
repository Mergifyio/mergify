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


LOG = daiquiri.getLogger(__name__)

DELAYED_REFRESH_KEY = "delayed-refresh"


async def plan_next_refresh(match: rules.RulesEvaluator, ctxt: context.Context) -> None:
    best_bet: typing.Optional[datetime.datetime] = None
    for rule in match.matching_rules:
        f = filter.NearDatetimeFilter(rule.conditions.extract_raw_filter_tree())
        bet = await f(ctxt.pull_request)
        if best_bet is None or best_bet > bet:
            best_bet = bet

    zset_subkey = f"{ctxt.repository.installation.owner_id}~{ctxt.repository.installation.owner_login}~{ctxt.repository.repo['id']}~{ctxt.repository.repo['name']}~{ctxt.pull['number']}"

    if best_bet is None or best_bet >= date.DT_MAX:
        await ctxt.redis.zrem(DELAYED_REFRESH_KEY, zset_subkey)
    else:
        await ctxt.redis.zadd(
            DELAYED_REFRESH_KEY,
            **{zset_subkey: best_bet.timestamp()},
        )


async def send(
    redis_stream: utils.RedisStream,
    redis_cache: utils.RedisCache,
) -> None:
    score = utils.utcnow().timestamp()
    for subkey in await redis_cache.zrangebyscore(DELAYED_REFRESH_KEY, "-inf", score):
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
        await worker.push(
            redis_stream,
            owner_id,
            owner_login,
            repository_id,
            repository_name,
            pull_request_number,
            "refresh",
            {
                "action": "internal",
                "ref": None,
            },  # type: ignore[typeddict-item]
        )

        await redis_cache.zrem(DELAYED_REFRESH_KEY, subkey)
