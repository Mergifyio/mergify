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
import collections
import typing

import daiquiri
import fastapi
from starlette import requests
from starlette import responses

# To load the json serializer need to read queues
from mergify_engine import check_api  # noqa
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import utils
from mergify_engine.clients import github as github_client
from mergify_engine.clients import http
from mergify_engine.queue import merge_train
from mergify_engine.web import auth
from mergify_engine.web import redis


LOG = daiquiri.getLogger(__name__)

router = fastapi.APIRouter()


@router.get(
    "/queues/{owner_id}",  # noqa: FS003
    dependencies=[fastapi.Depends(auth.signature_or_token)],
)
async def queues(
    request: requests.Request,
    owner_id: github_types.GitHubAccountIdType,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.Response:
    auth: typing.Optional[github_client.GithubTokenAuth]
    token = request.headers.get("Authorization")
    if token:
        token = token[6:]  # Drop 'token '
        auth = github_client.GithubTokenAuth(token)
        async with github_client.AsyncGithubInstallationClient(auth=auth) as client:
            # Check this token as access to this organization
            try:
                await client.item(f"/user/{owner_id}")
            except (http.HTTPNotFound, http.HTTPForbidden, http.HTTPUnauthorized):
                raise fastapi.HTTPException(status_code=403)
    else:
        auth = None

    queues: typing.Dict[
        str, typing.Dict[str, typing.List[int]]
    ] = collections.defaultdict(dict)
    async for queue in redis_cache.scan_iter(
        match=f"merge-*~{owner_id}~*", count=10000
    ):
        queue_type, _, repo_id, branch = queue.split("~")
        if auth is not None:
            # Check this token as access to this repository
            async with github_client.AsyncGithubInstallationClient(auth=auth) as client:
                try:
                    await client.item(f"/repositories/{repo_id}")
                except (http.HTTPNotFound, http.HTTPForbidden, http.HTTPUnauthorized):
                    continue

        if queue_type == "merge-queue":
            queues[repo_id][branch] = [
                int(pull) async for pull, _ in redis_cache.zscan_iter(queue)
            ]
        elif queue_type == "merge-train":
            train_raw = await redis_cache.get(queue)
            train = typing.cast(merge_train.Train.Serialized, json.loads(train_raw))
            _, _, repo_id, branch = queue.split("~")
            queues[repo_id][branch] = [
                int(ep[0])
                for c in train["cars"]
                for ep in c["still_queued_embarked_pulls"]
            ] + [int(wp[0]) for wp in train["waiting_pulls"]]

    return responses.JSONResponse(status_code=200, content=queues)
