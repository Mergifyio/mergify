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

import typing

import daiquiri
import fastapi
import sentry_sdk

from mergify_engine import config
from mergify_engine import context
from mergify_engine import exceptions
from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.dashboard import application as application_mod
from mergify_engine.dashboard import subscription
from mergify_engine.web import redis


LOG = daiquiri.getLogger(__name__)

ScopeT = typing.NewType("ScopeT", str)

security = fastapi.security.http.HTTPBearer()


async def get_application(
    request: fastapi.Request,
    credentials: fastapi.security.HTTPAuthorizationCredentials = fastapi.Security(  # noqa: B008
        security
    ),
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
) -> application_mod.Application:

    scope: typing.Optional[github_types.GitHubLogin] = request.path_params.get("owner")
    api_access_key = credentials.credentials[: config.API_ACCESS_KEY_LEN]
    api_secret_key = credentials.credentials[config.API_ACCESS_KEY_LEN :]
    try:
        app = await application_mod.Application.get(
            redis_links.cache, api_access_key, api_secret_key, scope
        )
    except application_mod.ApplicationUserNotFound:
        raise fastapi.HTTPException(status_code=403)

    # Seatbelt
    current_scope = None if app.account_scope is None else app.account_scope["login"]
    if scope is not None and (
        current_scope is None or current_scope.lower() != scope.lower()
    ):
        LOG.error(
            "got application with wrong scope",
            expected_scope=scope,
            current_scope=current_scope,
        )
        raise fastapi.HTTPException(status_code=403)
    return app


# Just an alias to help readability of fastapi.Depends
require_authentication = get_application


async def get_installation(
    application: application_mod.Application = fastapi.Depends(  # noqa: B008
        get_application
    ),
) -> github_types.GitHubInstallation:
    if application.account_scope is None:
        raise fastapi.HTTPException(status_code=403)

    try:
        return await github.get_installation_from_account_id(
            application.account_scope["id"]
        )
    except exceptions.MergifyNotInstalled:
        raise fastapi.HTTPException(status_code=403)


async def get_repository_context(
    owner: github_types.GitHubLogin = fastapi.Path(  # noqa: B008
        ..., description="The owner of the repository"
    ),
    repository: github_types.GitHubRepositoryName = fastapi.Path(  # noqa: B008
        ..., description="The name of the repository"
    ),
    redis_links: redis_utils.RedisLinks = fastapi.Depends(  # noqa: B008
        redis.get_redis_links
    ),
    installation_json: github_types.GitHubInstallation = fastapi.Depends(  # noqa: B008
        get_installation
    ),
) -> typing.AsyncGenerator[context.Repository, None]:
    async with github.aget_client(installation_json) as client:
        try:
            # Check this token has access to this repository
            repo = typing.cast(
                github_types.GitHubRepository,
                await client.item(f"/repos/{owner}/{repository}"),
            )
        except (http.HTTPNotFound, http.HTTPForbidden, http.HTTPUnauthorized):
            raise fastapi.HTTPException(status_code=404)

        sub = await subscription.Subscription.get_subscription(
            redis_links.cache, installation_json["account"]["id"]
        )

        installation = context.Installation(installation_json, sub, client, redis_links)

        repository_ctxt = installation.get_repository_from_github_data(repo)

        # NOTE(sileht): Since this method is used as fastapi Depends only, it's safe to set this
        # for the ongoing http request
        sentry_sdk.set_user({"username": repository_ctxt.installation.owner_login})
        sentry_sdk.set_tag("gh_owner", repository_ctxt.installation.owner_login)
        sentry_sdk.set_tag("gh_repo", repository_ctxt.repo["name"])

        yield repository_ctxt


async def check_subscription_feature_queue_freeze(
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        get_repository_context
    ),
) -> None:
    if not repository_ctxt.installation.subscription.has_feature(
        subscription.Features.QUEUE_FREEZE
    ):
        raise fastapi.HTTPException(
            status_code=403,
            detail="⚠ The subscription needs to be upgraded to enable the `queue_freeze` feature.",
        )


async def check_subscription_feature_eventlogs(
    repository_ctxt: context.Repository = fastapi.Depends(  # noqa: B008
        get_repository_context
    ),
) -> None:
    if repository_ctxt.installation.subscription.has_feature(
        subscription.Features.EVENTLOGS_LONG
    ) or repository_ctxt.installation.subscription.has_feature(
        subscription.Features.EVENTLOGS_SHORT
    ):
        return

    raise fastapi.HTTPException(
        status_code=402,
        detail="⚠ The subscription needs to be upgraded to enable the `eventlogs` feature.",
    )
