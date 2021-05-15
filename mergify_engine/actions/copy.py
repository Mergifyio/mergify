# -*- encoding: utf-8 -*-
#
#  Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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

import re
import typing
from urllib import parse

import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import signals
from mergify_engine import subscription
from mergify_engine.actions import utils as action_utils
from mergify_engine.clients import http
from mergify_engine.rules import types


def Regex(value: str) -> typing.Pattern[str]:
    try:
        return re.compile(value)
    except re.error as e:
        raise voluptuous.Invalid(str(e))


def DuplicateBodyJinja2(v):
    return types.Jinja2(
        v,
        {
            "destination_branch": "whatever",
            "cherry_pick_error": "whaever",
        },
    )


def DuplicateTitleJinja2(v):
    return types.Jinja2(
        v,
        {
            "destination_branch": "whatever",
        },
    )


class CopyAction(actions.Action):
    is_command: bool = True

    KIND: duplicate_pull.KindT = "copy"
    HOOK_EVENT_NAME: signals.EventName = "action.copy"
    BRANCH_PREFIX: str = "copy"
    SUCCESS_MESSAGE: str = "Pull request copies have been created"
    FAILURE_MESSAGE: str = "No copy have been created"

    @classmethod
    def get_config_schema(
        cls,
        partial_validation: bool = False,
    ) -> typing.Dict[typing.Any, typing.Any]:
        return {
            voluptuous.Required("bot_account", default=None): voluptuous.Any(
                None, types.Jinja2
            ),
            voluptuous.Required("branches", default=[]): [str],
            voluptuous.Required("regexes", default=[]): [voluptuous.Coerce(Regex)],
            voluptuous.Required("ignore_conflicts", default=True): bool,
            voluptuous.Required("assignees", default=[]): [types.Jinja2],
            voluptuous.Required("labels", default=[]): [str],
            voluptuous.Required("label_conflicts", default="conflicts"): str,
            voluptuous.Required(
                "title", default=f"{{{{ title }}}} ({cls.KIND} #{{{{ number }}}})"
            ): DuplicateTitleJinja2,
            voluptuous.Required(
                "body",
                default=f"This is an automatic {cls.KIND} of pull request #{{{{number}}}} done by [Mergify](https://mergify.io).\n{{{{ cherry_pick_error }}}}",
            ): DuplicateBodyJinja2,
        }

    @staticmethod
    def command_to_config(string: str) -> typing.Dict[str, typing.Any]:
        if string:
            return {"branches": string.split(" ")}
        else:
            return {}

    async def _copy(
        self, ctxt: context.Context, branch_name: github_types.GitHubRefType
    ) -> typing.Tuple[check_api.Conclusion, str]:
        """Copy the PR to a branch.

        Returns a tuple of strings (state, reason).
        """

        # NOTE(sileht): Ensure branch exists first
        escaped_branch_name = parse.quote(branch_name, safe="")
        try:
            await ctxt.client.item(f"{ctxt.base_url}/branches/{escaped_branch_name}")
        except http.HTTPStatusError as e:
            detail = f"{self.KIND.capitalize()} to branch `{branch_name}` failed: "
            if e.response.status_code >= 500:
                state = check_api.Conclusion.PENDING
            else:
                state = check_api.Conclusion.FAILURE
                detail += e.response.json()["message"]
            return state, detail

        # NOTE(sileht) does the duplicate have already been done ?
        new_pull = await self.get_existing_duplicate_pull(ctxt, branch_name)

        # No, then do it
        if not new_pull:
            try:
                users_to_add = await self.wanted_users(ctxt, self.config["assignees"])
                new_pull = await duplicate_pull.duplicate(
                    ctxt,
                    branch_name,
                    title_template=self.config["title"],
                    body_template=self.config["body"],
                    bot_account=self.config["bot_account"],
                    labels=self.config["labels"],
                    label_conflicts=self.config["label_conflicts"],
                    ignore_conflicts=self.config["ignore_conflicts"],
                    assignees=users_to_add,
                    kind=self.KIND,
                    branch_prefix=self.BRANCH_PREFIX,
                )
                await signals.send(
                    ctxt,
                    self.HOOK_EVENT_NAME,
                    {"bot_account": bool(self.config["bot_account"])},
                )

            except duplicate_pull.DuplicateAlreadyExists:
                new_pull = await self.get_existing_duplicate_pull(ctxt, branch_name)
            except duplicate_pull.DuplicateFailed as e:
                return (
                    check_api.Conclusion.FAILURE,
                    f"Backport to branch `{branch_name}` failed\n{e.reason}",
                )
            except duplicate_pull.DuplicateNotNeeded:
                return (
                    check_api.Conclusion.SUCCESS,
                    f"Backport to branch `{branch_name}` not needed, change already in branch `{branch_name}`",
                )
            except duplicate_pull.DuplicateUnexpectedError as e:
                ctxt.log.error(
                    "duplicate failed",
                    reason=e.reason,
                    branch=branch_name,
                    kind=self.KIND,
                    exc_info=True,
                )
                return (
                    check_api.Conclusion.FAILURE,
                    f"{self.KIND.capitalize()} to branch `{branch_name}` failed",
                )

        if new_pull:
            return (
                check_api.Conclusion.SUCCESS,
                f"[#{new_pull['number']} {new_pull['title']}]({new_pull['html_url']}) "
                f"has been created for branch `{branch_name}`",
            )

        # TODO(sileht): Should be safe to replace that by a RuntimeError now.
        # Just wait a couple of weeks (2020-03-03)
        ctxt.log.error(
            "unexpected %s to branch `%s` failure, "
            "duplicate() report pull copy already exists but it doesn't",
            self.KIND.capitalize(),
            branch_name,
        )

        return (
            check_api.Conclusion.FAILURE,
            f"{self.KIND.capitalize()} to branch `{branch_name}` failed",
        )

    async def run(
        self, ctxt: context.Context, rule: rules.EvaluatedRule
    ) -> check_api.Result:
        if not config.GITHUB_APP:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Unavailable with the GitHub Action",
                "Due to GitHub Action limitation, the `copy` action/command is only "
                "available with the Mergify GitHub App.",
            )

        if await ctxt.github_workflow_changed():
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                self.FAILURE_MESSAGE,
                "GitHub App like Mergify are not allowed to create pull request where `.github/workflows` is changed.",
            )

        template_result = await self._verify_template(ctxt)
        if template_result is not None:
            return template_result

        bot_account_result = await action_utils.validate_bot_account(
            ctxt,
            self.config["bot_account"],
            required_feature=subscription.Features.BOT_ACCOUNT,
            missing_feature_message=f"{self.KIND.capitalize()} with `bot_account` set is unavailable",
        )
        if bot_account_result is not None:
            return bot_account_result

        branches: typing.List[github_types.GitHubRefType] = self.config["branches"]
        if self.config["regexes"]:
            branches.extend(
                [
                    branch["name"]
                    async for branch in typing.cast(
                        typing.AsyncGenerator[github_types.GitHubBranch, None],
                        ctxt.client.items(f"{ctxt.base_url}/branches"),
                    )
                    if any(
                        map(
                            lambda regex: regex.match(branch["name"]),
                            self.config["regexes"],
                        )
                    )
                ]
            )

        if len(branches) == 0:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                self.FAILURE_MESSAGE,
                "No destination branches found",
            )

        results = [await self._copy(ctxt, branch_name) for branch_name in branches]

        # Pick the first status as the final_status
        conclusion = results[0][0]
        for r in results[1:]:
            if r[0] == check_api.Conclusion.FAILURE:
                conclusion = check_api.Conclusion.FAILURE
                # If we have a failure, everything is set to fail
                break
            elif r[0] == check_api.Conclusion.SUCCESS:
                # If it was None, replace with success
                # Keep checking for a failure just in case
                conclusion = check_api.Conclusion.SUCCESS

        if conclusion == check_api.Conclusion.SUCCESS:
            message = self.SUCCESS_MESSAGE
        elif conclusion == check_api.Conclusion.FAILURE:
            message = self.FAILURE_MESSAGE
        else:
            message = "Pending"

        return check_api.Result(
            conclusion,
            message,
            "\n".join(f"* {detail}" for detail in (r[1] for r in results)),
        )

    @classmethod
    async def get_existing_duplicate_pull(
        cls, ctxt: context.Context, branch_name: github_types.GitHubRefType
    ) -> typing.Optional[github_types.GitHubPullRequest]:
        bp_branch = duplicate_pull.get_destination_branch_name(
            ctxt.pull["number"], branch_name, cls.BRANCH_PREFIX
        )
        pulls = [
            pull
            async for pull in typing.cast(
                typing.AsyncGenerator[github_types.GitHubPullRequest, None],
                ctxt.client.items(
                    f"{ctxt.base_url}/pulls",
                    params={
                        "base": branch_name,
                        "sort": "created",
                        "state": "all",
                        "head": f"{ctxt.pull['base']['user']['login']}:{bp_branch}",
                    },
                ),
            )
        ]

        return pulls[-1] if pulls else None

    async def _verify_template(
        self, ctxt: context.Context
    ) -> typing.Optional[check_api.Result]:
        try:
            await ctxt.pull_request.render_template(
                self.config["title"],
                extra_variables={"destination_branch": "whatever"},
            )
        except context.RenderTemplateFailure as rmf:
            # can't occur, template have been checked earlier
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                self.FAILURE_MESSAGE,
                f"Invalid title message: {rmf}",
            )

        try:
            await ctxt.pull_request.render_template(
                self.config["body"],
                extra_variables={
                    "destination_branch": "whatever",
                    "cherry_pick_error": "whatever",
                },
            )
        except context.RenderTemplateFailure as rmf:
            # can't occur, template have been checked earlier
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                self.FAILURE_MESSAGE,
                f"Invalid body message: {rmf}",
            )
        return None
