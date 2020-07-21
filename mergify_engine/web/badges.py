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


import fastapi
from starlette import responses


app = fastapi.FastAPI()


def _get_badge_url(owner, repo, ext, style):
    return responses.RedirectResponse(
        url=f"https://img.shields.io/endpoint.{ext}?url=https://dashboard.mergify.io/badges/{owner}/{repo}&style={style}",
        status_code=302,
    )


@app.get("/{owner}/{repo}.png")
async def badge_png(owner, repo, style: str = "flat"):  # pragma: no cover
    return _get_badge_url(owner, repo, "png", style)


@app.get("/{owner}/{repo}.svg")
async def badge_svg(owner, repo, style: str = "flat"):  # pragma: no cover
    return _get_badge_url(owner, repo, "svg", style)


@app.get("/{owner}/{repo}")
async def badge(owner, repo):
    return responses.RedirectResponse(
        url=f"https://dashboard.mergify.io/badges/{owner}/{repo}"
    )
