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
import functools
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


class PullRequestUrlInvalid(voluptuous.Invalid):
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


def ensure_no_voluptuous(value):
    if isinstance(value, (dict, list, str)):
        return value
    else:
        return str(value)


def voluptuous_error(error):
    return {
        "type": error.__class__.__name__,
        "message": error.error_message,
        "error": str(error),
        "details": list(map(ensure_no_voluptuous, error.path)),
    }


@app.exception_handler(voluptuous.Invalid)
async def voluptuous_errors(request: requests.Request, exc: voluptuous.Invalid):
    # FIXME(sileht): remove error at payload root
    payload = voluptuous_error(exc)
    payload["errors"] = []
    if isinstance(exc, voluptuous.MultipleInvalid):
        payload["errors"].extend(map(voluptuous_error, sorted(exc.errors, key=str)))
    else:
        payload["errors"].extend(voluptuous_error(exc))
    return responses.JSONResponse(status_code=400, content=payload)


def _sync_simulator(pull_request_rules, owner, repo, pull_number, token):
    try:
        if token:
            auth = github.GithubTokenAuth(owner, token)
        else:
            auth = github.get_auth(owner)

        with github.get_client(auth=auth) as client:
            try:
                data = client.item(f"/repos/{owner}/{repo}/pulls/{pull_number}")
            except http.HTTPNotFound:
                raise PullRequestUrlInvalid(
                    message=f"Pull request {owner}/{repo}/pulls/{pull_number} not found"
                )

            sub = asyncio.run(
                subscription.Subscription.get_subscription(client.auth.owner_id)
            )

            ctxt = context.Context(
                client,
                data,
                sub,
                [{"event_type": "mergify-simulator", "data": []}],
            )
            match = pull_request_rules.get_pull_request_rule(ctxt)
            return actions_runner.gen_summary(ctxt, match)
    except exceptions.MergifyNotInstalled:
        raise PullRequestUrlInvalid(
            message=f"Mergify not installed on repository '{owner}/{repo}'"
        )


@app.post("/", dependencies=[fastapi.Depends(auth.signature_or_token)])
async def simulator(request: requests.Request):
    token = request.headers.get("Authorization")
    if token:
        token = token[6:]  # Drop 'token '

    data = SimulatorSchema(await request.json())
    if data["pull_request"]:
        loop = asyncio.get_running_loop()
        title, summary = await loop.run_in_executor(
            None,
            functools.partial(
                _sync_simulator,
                data["mergify.yml"]["pull_request_rules"],
                *data["pull_request"],
                token=token,
            ),
        )
    else:
        title, summary = ("The configuration is valid", None)

    return responses.JSONResponse(
        status_code=200, content={"title": title, "summary": summary}
    )
