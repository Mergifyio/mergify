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

import datetime
import hashlib
import typing
import uuid

import daiquiri
import yaaredis.exceptions

from mergify_engine import github_types
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)

ScriptIdT = typing.NewType("ScriptIdT", uuid.UUID)

SCRIPTS: typing.Dict[ScriptIdT, typing.Tuple[str, str]] = {}


def register_script(script: str) -> ScriptIdT:
    global SCRIPTS
    # NOTE(sileht): We don't use sha, in case of something server side change the script sha
    script_id = ScriptIdT(uuid.uuid4())
    SCRIPTS[script_id] = (
        hashlib.sha1(script.encode("utf8")).hexdigest(),  # nosec
        script,
    )
    return script_id


async def run_script(
    redis: utils.RedisStream,
    script_id: ScriptIdT,
    keys: typing.Tuple[str, ...],
    args: typing.Optional[typing.Tuple[typing.Union[str], ...]] = None,
) -> typing.Any:
    global SCRIPTS
    sha, script = SCRIPTS[script_id]
    if args is None:
        args = keys
    else:
        args = keys + args
    try:
        return await redis.evalsha(sha, len(keys), *args)
    except yaaredis.exceptions.NoScriptError:
        newsha = await redis.script_load(script)
        if newsha != sha:
            LOG.error(
                "wrong redis script sha cached",
                script_id=script_id,
                sha=sha,
                newsha=newsha,
            )
            SCRIPTS[script_id] = (newsha, script)
        return await redis.evalsha(newsha, len(keys), *args)


PUSH_PR_SCRIPT = register_script(
    """
local bucket_key = KEYS[1]
local bucket_sources_key = KEYS[2]
local scheduled_at_timestamp = ARGV[1]
local source = ARGV[2]
local score = ARGV[3]

-- Add the source to the pull request sources
redis.call("XADD", bucket_sources_key, "*", "source", source, "score", score)
-- Add this pull request to the org bucket if not exists
-- REDIS 6.2:
-- redis.call("ZADD", bucket_key, "LT", score, bucket_sources_key)
-- REDIS < 6.2:
local old_score = tonumber(redis.call("ZSCORE", bucket_key, bucket_sources_key))
if (old_score == nil) or (tonumber(score) < old_score) then
   redis.call("ZADD", bucket_key, score, bucket_sources_key)
end
-- Add the org bucket to the stream list
redis.call("ZADD", "streams", "NX", scheduled_at_timestamp, bucket_key)
"""
)


async def push_pull(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
    repo_id: github_types.GitHubRepositoryIdType,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: typing.Optional[github_types.GitHubPullRequestNumber],
    scheduled_at: datetime.datetime,
    source: str,
    score: str,
) -> None:
    await run_script(
        redis,
        PUSH_PR_SCRIPT,
        (
            f"bucket~{owner_id}~{owner_login}",
            f"bucket-sources~{repo_id}~{repo_name}~{pull_number or 0}",
        ),
        (
            str(scheduled_at.timestamp()),
            source,
            score,
        ),
    )


REMOVE_PR_SCRIPT = register_script(
    """
local bucket_key = KEYS[1]
local bucket_sources_key = KEYS[2]
-- Delete all sources we have handled
-- Check if sources has been received in the meantime
for idx, msg_id in ipairs(ARGV) do
    redis.call("XDEL", bucket_sources_key, msg_id)
end
local sources = redis.call("XRANGE", bucket_sources_key, "-", "+", "COUNT", 1)
if table.getn(sources) == 0 then
    redis.call("DEL", bucket_sources_key)
    -- No new source, drop this pull request bucket
    redis.call("ZREM", bucket_key, bucket_sources_key)
    -- No need to clean "streams" key, CLEAN_STREAM_SCRIPT is always
    -- called at the end and it will do it
else
    -- Yes new sources are waiting, update the score for this pull request with
    -- the next one
    -- FIXME(sileht): if this is an unpacked push event we should pick another
    -- one
    -- For the first version, this is not a big issue, if the pull request is
    -- really active, another event will override the score with a lower one.
    local score = sources[1][2][4]
    redis.call("ZADD", bucket_key, score, bucket_sources_key)
end
"""
)


async def remove_pull(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
    repo_id: github_types.GitHubRepositoryIdType,
    repo_name: github_types.GitHubRepositoryName,
    pull_number: typing.Optional[github_types.GitHubPullRequestNumber],
    message_ids: typing.Tuple[str, ...],
) -> None:
    await run_script(
        redis,
        REMOVE_PR_SCRIPT,
        (
            f"bucket~{owner_id}~{owner_login}",
            f"bucket-sources~{repo_id}~{repo_name}~{pull_number or 0}",
        ),
        message_ids,
    )


# TODO(sileht): limited to 7999 keys, should be OK for now, if we have an issue
# just paginate the ZRANGE
DROP_BUCKET_SCRIPT = register_script(
    """
local bucket_key = KEYS[1]
local members = redis.call("ZRANGE", bucket_key, 0, -1)
redis.call("DEL", unpack(members))
redis.call("DEL", bucket_key)
-- No need to clean "streams" key, CLEAN_STREAM_SCRIPT is always
-- called at the end and it will do it
"""
)


async def drop_bucket(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
) -> None:
    await run_script(redis, DROP_BUCKET_SCRIPT, (f"bucket~{owner_id}~{owner_login}",))


# NOTE(sileht): If the stream/buckets still have events, we update the score to
# reschedule the pull later
CLEAN_STREAM_SCRIPT = register_script(
    """
local bucket_key = KEYS[1]
local scheduled_at_timestamp = ARGV[1]
redis.call("HDEL", "attempts", bucket_key)
if redis.call("ZCARD", bucket_key) == 0 then
    redis.call("ZREM", "streams", bucket_key)
else
    redis.call("ZADD", "streams", scheduled_at_timestamp, bucket_key)
end
"""
)


async def clean_org_bucket(
    redis: utils.RedisStream,
    owner_id: github_types.GitHubAccountIdType,
    owner_login: github_types.GitHubLogin,
    scheduled_at: datetime.datetime,
) -> None:
    await run_script(
        redis,
        CLEAN_STREAM_SCRIPT,
        (f"bucket~{owner_id}~{owner_login}",),
        (str(scheduled_at.timestamp()),),
    )


MIGRATE_TRAINS = register_script(
    """
local trains = redis.call("KEYS", "merge-train~*~*~*")
for idx, train in ipairs(trains) do
    for owner_id, repo_id, ref in string.gmatch(train, "merge%-train~([^~]+)~([^~]+)~(.+)") do
        local data = redis.call("GET", train)
        redis.call("HSET", "merge-trains~" .. owner_id, repo_id .. "~" .. ref, data)
        redis.call("DEL", train)
    end
end
"""
)


async def migrate_trains(
    redis: utils.RedisCache,
) -> None:
    await run_script(redis, MIGRATE_TRAINS, ())
