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


import base64
import hashlib

import fastapi
from starlette import responses
from starlette.middleware import cors

from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules


app = fastapi.FastAPI()
app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/")
async def config_validator(
    data: fastapi.UploadFile = fastapi.File(...),  # noqa: B008
) -> responses.PlainTextResponse:  # pragma: no cover
    try:
        bytes_or_str = await data.read()

        if isinstance(bytes_or_str, str):
            content_bytes = bytes_or_str.encode()
        else:
            content_bytes = bytes_or_str

        rules.get_mergify_config(
            context.MergifyConfigFile(
                {
                    "path": data.filename,
                    "type": "file",
                    "content": base64.b64encode(content_bytes).decode(),
                    "decoded_content": content_bytes,
                    "sha": github_types.SHAType(
                        hashlib.sha1(content_bytes).hexdigest()  # nosec
                    ),
                }
            )
        )
    except Exception as e:
        status = 400
        message = str(e)
    else:
        status = 200
        message = "The configuration is valid"

    return responses.PlainTextResponse(message, status_code=status)
