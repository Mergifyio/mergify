# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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
import json
import typing
from urllib.parse import urlsplit

import fastapi
from starlette import requests
from starlette import responses
from starlette.middleware import cors
import voluptuous

from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner
from mergify_engine.web import auth
from mergify_engine.web import redis


app = fastapi.FastAPI()
app.add_middleware(
    cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PullRequestUrlInvalid(voluptuous.Invalid):  # type: ignore[misc]
    pass


@voluptuous.message("expected a Pull Request URL", cls=PullRequestUrlInvalid)
def PullRequestUrl(v):
    _, owner, repo, _, pull_number = urlsplit(v).path.split("/")
    pull_number = int(pull_number)
    return owner, repo, pull_number


def SimulatorMergifyConfig(v: bytes) -> rules.MergifyConfig:
    try:
        return rules.get_mergify_config(
            context.MergifyConfigFile(
                {
                    "type": "file",
                    "content": "whatever",
                    "sha": github_types.SHAType("whatever"),
                    "path": ".mergify.yml",
                    "decoded_content": v,
                }
            )
        )
    except rules.InvalidRules as e:
        raise e.error


SimulatorSchema = voluptuous.Schema(
    {
        voluptuous.Required("pull_request"): voluptuous.Any(None, PullRequestUrl()),
        voluptuous.Required("mergify.yml"): voluptuous.Coerce(SimulatorMergifyConfig),
    }
)


def voluptuous_error(error: voluptuous.Invalid) -> str:
    if error.path:
        if error.path[0] == "mergify.yml":
            error.path.pop(0)
    return str(rules.InvalidRules(error, ""))


@app.exception_handler(voluptuous.Invalid)
async def voluptuous_errors(
    request: requests.Request, exc: voluptuous.Invalid
) -> responses.JSONResponse:
    # Replace payload by our own
    if isinstance(exc, voluptuous.MultipleInvalid):
        payload = {"errors": list(map(voluptuous_error, sorted(exc.errors, key=str)))}
    else:
        payload = {"errors": [voluptuous_error(exc)]}
    return responses.JSONResponse(status_code=400, content=payload)


async def _simulator(
    redis_cache: utils.RedisCache,
    pull_request_rules: rules.PullRequestRules,
    owner: github_types.GitHubLogin,
    repo: str,
    pull_number: int,
    token: str,
) -> typing.Tuple[str, str]:
    try:
        auth: typing.Union[
            github.GithubAppInstallationAuth,
            github.GithubActionAccessTokenAuth,
            github.GithubTokenAuth,
        ]
        if token:
            auth = github.GithubTokenAuth(token)
        else:
            auth = github.get_auth(owner)

        async with github.aget_client(auth=auth) as client:
            try:
                data = await client.item(f"/repos/{owner}/{repo}/pulls/{pull_number}")
            except http.HTTPNotFound:
                raise PullRequestUrlInvalid(
                    message=f"Pull request {owner}/{repo}/pulls/{pull_number} not found"
                )

            sub = await subscription.Subscription.get_subscription(
                redis_cache, data["base"]["user"]["id"]
            )

            installation = context.Installation(
                data["base"]["user"]["id"],
                owner,
                sub,
                client,
                redis_cache,
            )
            repository = context.Repository(installation, data["base"]["repo"])
            ctxt = await repository.get_pull_request_context(data["number"], data)
            ctxt.sources = [{"event_type": "mergify-simulator", "data": [], "timestamp": ""}]  # type: ignore[typeddict-item]
            match = await pull_request_rules.get_pull_request_rule(ctxt)
            return await actions_runner.gen_summary(ctxt, pull_request_rules, match)
    except exceptions.MergifyNotInstalled:
        raise PullRequestUrlInvalid(
            message=f"Mergify not installed on repository '{owner}/{repo}'"
        )


@app.post("/", dependencies=[fastapi.Depends(auth.signature_or_token)])
async def simulator(
    request: requests.Request,
    redis_cache: utils.RedisCache = fastapi.Depends(  # noqa: B008
        redis.get_redis_cache
    ),
) -> responses.JSONResponse:
    token = request.headers.get("Authorization")
    if token:
        token = token[6:]  # Drop 'token '

    try:
        raw_json = await request.json()
    except json.JSONDecodeError:
        return responses.JSONResponse(status_code=400, content="invalid json")

    data = SimulatorSchema(raw_json)

    if data["pull_request"]:
        title, summary = await _simulator(
            redis_cache,
            data["mergify.yml"]["pull_request_rules"],
            owner=data["pull_request"][0],
            repo=data["pull_request"][1],
            pull_number=data["pull_request"][2],
            token=token,
        )
    else:
        title, summary = ("The configuration is valid", "")

    return responses.JSONResponse(
        status_code=200,
        content={
            "title": title,
            "summary": summary,
        },
    )
