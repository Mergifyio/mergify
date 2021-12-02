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
import yaaredis.exceptions

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
