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


import asyncio
from urllib.parse import urlsplit

import fastapi
from starlette import requests
from starlette import responses
from starlette.middleware import cors
import voluptuous

from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import web
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.engine import actions_runner
from mergify_engine.web import auth


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


SimulatorSchema = voluptuous.Schema(
    {
        voluptuous.Required("pull_request"): voluptuous.Any(None, PullRequestUrl()),
        voluptuous.Required("mergify.yml"): rules.UserConfigurationSchema,
    }
)


def voluptuous_error(error):
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


async def _simulator(pull_request_rules, owner, repo, pull_number, token):
    try:
        if token:
            auth = github.GithubTokenAuth(owner, token)
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
                web._AREDIS_CACHE, client.auth.owner_id
            )

            installation = context.Installation(
                client.auth.owner_id,
                owner,
                sub,
                client,
                web._AREDIS_CACHE,
            )
            repository = context.Repository(installation, repo)
            ctxt = await repository.get_pull_request_context(data["number"], data)
            ctxt.sources = ([{"event_type": "mergify-simulator", "data": []}],)
            match = await pull_request_rules.get_pull_request_rule(ctxt)
            return actions_runner.gen_summary(ctxt, match)
    except exceptions.MergifyNotInstalled:
        raise PullRequestUrlInvalid(
            message=f"Mergify not installed on repository '{owner}/{repo}'"
        )


@app.post("/", dependencies=[fastapi.Depends(auth.signature_or_token)])
async def simulator(request: requests.Request) -> responses.JSONResponse:
    token = request.headers.get("Authorization")
    if token:
        token = token[6:]  # Drop 'token '

    data = SimulatorSchema(await request.json())
    if data["pull_request"]:
        # TODO(sileht): remove threads when all httpx call are async
        loop = asyncio.get_running_loop()
        title, summary = await loop.run_in_executor(  # type: ignore
            None,
            asyncio.run,
            _simulator(
                data["mergify.yml"]["pull_request_rules"],
                owner=data["pull_request"][0],
                repo=data["pull_request"][1],
                pull_number=data["pull_request"][2],
                token=token,
            ),
        )
    else:
        title, summary = ("The configuration is valid", None)

    return responses.JSONResponse(
        status_code=200, content={"title": title, "summary": summary}
    )
