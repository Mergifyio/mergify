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

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.rules import filter


def get_redis_key_for(
    owner_id: github_types.GitHubAccountIdType,
) -> str:
    return f"delayed-refresh~{owner_id}"


_DAYS: typing.List[typing.Tuple[str, str]] = [
    ("Monday", "Mon"),
    ("Tuesday", "Tue"),
    ("Wednesday", "Wed"),
    ("Thursday", "Thu"),
    ("Friday", "Fri"),
    ("Saturday", "Sat"),
    ("Sunday", "Sun"),
]


async def _get_next_matching_day(cond: filter.Filter) -> typing.Optional[int]:
    now = utils.utcnow()
    next_matching_day = None
    for i, days in enumerate(_DAYS):
        day_number = i + 1
        if day_number == now.day:
            # No need to reevaluate today
            continue
        elif await cond(utils.FakePR("days", days)):
            if next_matching_day is None:
                next_matching_day = day_number
            elif day_number > now.day and next_matching_day > day_number:
                next_matching_day = day_number
            elif day_number < now.day and next_matching_day > day_number + 7:
                next_matching_day = day_number + 7
    return next_matching_day


async def plan_next_refresh(match: rules.RulesEvaluator, ctxt: context.Context) -> None:
    now = utils.utcnow()
    best_bet: typing.Optional[datetime.datetime] = None
    for rule in match.matching_rules:
        for cond in rule.conditions:

            if cond.get_attribute_name() == "time":
                time = cond.get_attribute_value()
                bet = now.replace(minute=time.minute, second=time.second)
                if time >= now.time():
                    bet = bet + datetime.timedelta(days=1)
                if best_bet is None or best_bet > bet:
                    best_bet = bet

            elif cond.attribute_name == "days":
                next_matching_day = await _get_next_matching_day(cond)
                if next_matching_day is not None:
                    bet = now.replace(
                        day=next_matching_day, hour=0, minute=0, second=0, microsecond=0
                    )

                    if best_bet is None or best_bet > bet:
                        best_bet = bet

    zset_subkey = f"{ctxt.repository.repo['id']}~{ctxt.pull['number']}"
    if best_bet is None:
        await ctxt.redis.zrem(
            get_redis_key_for(ctxt.repository.installation.owner_id), zset_subkey
        )
    else:
        zset_key = get_redis_key_for(ctxt.repository.installation.owner_id)
        await ctxt.redis.zaddoption(
            zset_key,
            **{zset_subkey: best_bet.timestamp()},
        )


async def send(
    redis_stream: utils.RedisStream,
    installation: context.Installation,
) -> None:
    score = utils.utcnow().timestamp()
    key = get_redis_key_for(installation.owner_id)
    for subkey in await installation.redis.zrangebyscore(key, "-inf", score):
        repository_id_str, pull_request_number_str = subkey.split("~")
        repository_id = github_types.GitHubRepositoryIdType(int(repository_id_str))
        pull_request_number = github_types.GitHubPullRequestNumber(
            int(pull_request_number_str)
        )
        repository = await installation.get_repository_by_id(repository_id)
        await utils.send_refresh(
            redis_cache=installation.redis,
            redis_stream=redis_stream,
            repository=repository.repo,
            pull_request_number=pull_request_number,
        )
        await installation.redis.zrem(key, subkey)
