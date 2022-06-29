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

import dataclasses
import typing

import voluptuous

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.rules import types


GitHubLoginSchema = voluptuous.Schema(types.GitHubLogin)


@dataclasses.dataclass
class RenderBotAccountFailure(Exception):
    status: check_api.Conclusion
    title: str
    reason: str


async def render_bot_account(
    ctxt: context.Context,
    bot_account_template: typing.Optional[str],
    *,
    option_name: str = "bot_account",
    required_feature: subscription.Features,
    missing_feature_message: str = "Cannot use `bot_account`",
    required_permissions: typing.Optional[
        typing.List[github_types.GitHubRepositoryPermission]
    ] = None,
) -> typing.Optional[github_types.GitHubLogin]:
    if bot_account_template is None:
        return None

    if required_feature is not None and not ctxt.subscription.has_feature(
        required_feature
    ):
        raise RenderBotAccountFailure(
            check_api.Conclusion.ACTION_REQUIRED,
            missing_feature_message,
            ctxt.subscription.missing_feature_reason(
                ctxt.pull["base"]["repo"]["owner"]["login"]
            ),
        )

    if required_permissions is None:
        required_permissions = ["admin", "write", "maintain"]

    try:
        bot_account = await ctxt.pull_request.render_template(bot_account_template)
    except context.RenderTemplateFailure as rmf:
        raise RenderBotAccountFailure(
            check_api.Conclusion.FAILURE,
            f"Invalid {option_name} template",
            str(rmf),
        )

    try:
        bot_account = typing.cast(
            github_types.GitHubLogin, GitHubLoginSchema(bot_account)
        )
    except voluptuous.Invalid as e:
        raise RenderBotAccountFailure(
            check_api.Conclusion.FAILURE,
            f"Invalid {option_name} value",
            str(e),
        )

    if required_permissions:
        try:
            user = await ctxt.repository.installation.get_user(bot_account)
            permission = await ctxt.repository.get_user_permission(user)
        except http.HTTPNotFound:
            raise RenderBotAccountFailure(
                check_api.Conclusion.ACTION_REQUIRED,
                f"User `{bot_account}` used as `{option_name}` is unknown",
                f"Please make sure `{bot_account}` exists and has logged in [Mergify dashboard](https://dashboard.mergify.com).",
            )

        if permission not in required_permissions:
            quoted_required_permissions = [f"`{p}`" for p in required_permissions]
            if len(quoted_required_permissions) == 1:
                fancy_perm = quoted_required_permissions[0]
            else:
                fancy_perm = ", ".join(quoted_required_permissions[0:-1])
                fancy_perm += f" or {quoted_required_permissions[-1]}"
            required_permissions[0:-1]
            # `write` or `maintain`
            raise RenderBotAccountFailure(
                check_api.Conclusion.ACTION_REQUIRED,
                (
                    f"`{bot_account}` account used as "
                    f"`{option_name}` must have {fancy_perm} permission, "
                    f"not `{permission}`"
                ),
                "",
            )

    return bot_account
