#
# Copyright © 2019–2021 Mergify SAS
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

import hashlib
import typing
import uuid

import daiquiri

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


async def load_script(redis: utils.RedisStream, script_id: ScriptIdT) -> None:
    global SCRIPTS
    sha, script = SCRIPTS[script_id]
    newsha = await redis.script_load(script)
    if newsha != sha:
        LOG.error(
            "wrong redis script sha cached",
            script_id=script_id,
            sha=sha,
            newsha=newsha,
        )
        SCRIPTS[script_id] = (newsha, script)


async def load_scripts(redis: utils.RedisStream) -> None:
    # TODO(sileht): cleanup unused script, this is tricky, because during
    # deployment we have running in parallel due to the rolling upgrade:
    # * an old version of the asgi server
    # * a new version of the asgi server
    # * a new version of the backend
    global SCRIPTS
    ids = list(SCRIPTS.keys())
    exists = await redis.script_exists(*ids)
    for script_id, exist in zip(ids, exists):
        if not exist:
            await load_script(redis, script_id)


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
    return await redis.evalsha(sha, len(keys), *args)
