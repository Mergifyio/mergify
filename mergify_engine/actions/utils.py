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

from mergify_engine import check_api
from mergify_engine import context
from mergify_engine import subscription


async def validate_bot_account(
    ctxt: context.Context,
    bot_account: typing.Optional[str],
    *,
    option_name: str = "bot_account",
    # TODO(sileht): make it mandatory when all bot_account need subscription
    required_feature: typing.Optional[subscription.Features],
    missing_feature_message: str = "This action with `bot_account` set is unavailable",
    need_write_permission: bool = True,
) -> typing.Optional[check_api.Result]:
    if bot_account is None:
        return None

    if required_feature is not None and not ctxt.subscription.has_feature(
        required_feature
    ):
        return check_api.Result(
            check_api.Conclusion.ACTION_REQUIRED,
            missing_feature_message,
            ctxt.subscription.missing_feature_reason(
                ctxt.pull["base"]["repo"]["owner"]["login"]
            ),
        )

    if need_write_permission:
        # TODO(sileht): Cache this, people only use one bot account!
        permission = (
            await ctxt.client.item(
                f"{ctxt.base_url}/collaborators/{bot_account}/permission"
            )
        )["permission"]
        if permission not in ("write", "maintain"):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                (
                    f"`{bot_account}` account used as "
                    f"`{option_name}` must have `write` or `maintain` permission, "
                    f"not `{permission}`"
                ),
                "",
            )

    return None
