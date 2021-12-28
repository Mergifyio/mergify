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
import typing

import daiquiri
from ddtrace import tracer

from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)

BucketOrgKeyType = typing.NewType("BucketOrgKeyType", str)
BucketSourcesKeyType = typing.NewType("BucketSourcesKeyType", str)

PUSH_PR_SCRIPT = redis_utils.register_script(
    """
local bucket_org_key = KEYS[1]
local bucket_sources_key = KEYS[2]
local scheduled_at_timestamp = ARGV[1]
local source = ARGV[2]
local score = ARGV[3]
local repo_name = ARGV[4]

-- Add the source to the pull request sources
redis.call("XADD", bucket_sources_key, "*", "source", source, "score", score, "repo_name", repo_name)
-- Add this pull request to the org bucket if not exists
-- REDIS 6.2:
-- redis.call("ZADD", bucket_org_key, "LT", score, bucket_sources_key)
-- REDIS < 6.2:
local old_score = tonumber(redis.call("ZSCORE", bucket_org_key, bucket_sources_key))
if (old_score == nil) or (tonumber(score) < old_score) then
   redis.call("ZADD", bucket_org_key, score, bucket_sources_key)
end
-- Add the org bucket to the stream list
redis.call("ZADD", "streams", "NX", scheduled_at_timestamp, bucket_org_key)
"""
)


@tracer.wrap("stream_push_pull", span_type="worker")
async def push_pull(
    redis: utils.RedisStream,
    bucket_org_key: BucketOrgKeyType,
    bucket_sources_key: BucketSourcesKeyType,
    tracing_repo_name: github_types.GitHubRepositoryNameForTracing,
    scheduled_at: datetime.datetime,
    source: str,
    score: str,
) -> None:
    await redis_utils.run_script(
        redis,
        PUSH_PR_SCRIPT,
        (
            bucket_org_key,
            bucket_sources_key,
        ),
        (
            str(scheduled_at.timestamp()),
            source,
            score,
            tracing_repo_name,
        ),
    )


REMOVE_PR_SCRIPT = redis_utils.register_script(
    """
local bucket_org_key = KEYS[1]
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
    redis.call("ZREM", bucket_org_key, bucket_sources_key)
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
    redis.call("ZADD", bucket_org_key, score, bucket_sources_key)
end
"""
)


@tracer.wrap("stream_remove_pull", span_type="worker")
async def remove_pull(
    redis: utils.RedisStream,
    bucket_org_key: BucketOrgKeyType,
    bucket_sources_key: BucketSourcesKeyType,
    message_ids: typing.Tuple[str, ...],
) -> None:
    await redis_utils.run_script(
        redis,
        REMOVE_PR_SCRIPT,
        (
            bucket_org_key,
            bucket_sources_key,
        ),
        message_ids,
    )


# TODO(sileht): limited to 7999 keys, should be OK for now, if we have an issue
# just paginate the ZRANGE
DROP_BUCKET_SCRIPT = redis_utils.register_script(
    """
local bucket_org_key = KEYS[1]
local members = redis.call("ZRANGE", bucket_org_key, 0, -1)
redis.call("DEL", unpack(members))
redis.call("DEL", bucket_org_key)
-- No need to clean "streams" key, CLEAN_STREAM_SCRIPT is always
-- called at the end and it will do it
"""
)


@tracer.wrap("stream_drop_bucket", span_type="worker")
async def drop_bucket(
    redis: utils.RedisStream,
    bucket_org_key: BucketOrgKeyType,
) -> None:
    await redis_utils.run_script(redis, DROP_BUCKET_SCRIPT, (bucket_org_key,))


# NOTE(sileht): If the stream/buckets still have events, we update the score to
# reschedule the pull later
CLEAN_STREAM_SCRIPT = redis_utils.register_script(
    """
local bucket_org_key = KEYS[1]
local scheduled_at_timestamp = ARGV[1]
redis.call("HDEL", "attempts", bucket_org_key)
if redis.call("ZCARD", bucket_org_key) == 0 then
    redis.call("ZREM", "streams", bucket_org_key)
else
    redis.call("ZADD", "streams", scheduled_at_timestamp, bucket_org_key)
end
"""
)


@tracer.wrap("stream_clean_org_bucket", span_type="worker")
async def clean_org_bucket(
    redis: utils.RedisStream,
    bucket_org_key: BucketOrgKeyType,
    scheduled_at: datetime.datetime,
) -> None:
    await redis_utils.run_script(
        redis,
        CLEAN_STREAM_SCRIPT,
        (bucket_org_key,),
        (str(scheduled_at.timestamp()),),
    )
