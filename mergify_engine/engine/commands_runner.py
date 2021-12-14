# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2021 Mergify SAS
#
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
import json
import re
import typing

import daiquiri
from datadog import statsd
import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine.clients import github
from mergify_engine.clients import http
from mergify_engine.rules import conditions


LOG = daiquiri.getLogger(__name__)

COMMAND_MATCHER = re.compile(r"@Mergify(?:|io) (\w*)(.*)", re.IGNORECASE)
COMMAND_RESULT_MATCHER_OLD = re.compile(
    r"\*Command `([^`]*)`: (pending|success|failure)\*"
)
COMMAND_RESULT_MATCHER = re.compile(
    r"^-\*- Mergify Payload -\*-\n(.+)\n^-\*- Mergify Payload End -\*-",
    re.MULTILINE,
)

MERGE_QUEUE_COMMAND_MESSAGE = "Command not allowed on merge queue pull request."
UNKNOWN_COMMAND_MESSAGE = "Sorry but I didn't understand the command. Please consult [the commands documentation](https://docs.mergify.com/commands.html) \U0001F4DA."
INVALID_COMMAND_ARGS_MESSAGE = "Sorry but I didn't understand the arguments of the command `{command}`. Please consult [the commands documentation](https://docs.mergify.com/commands/) \U0001F4DA."  # noqa
WRONG_ACCOUNT_MESSAGE = "_Hey, I reacted but my real name is @Mergifyio_"
CONFIGURATION_CHANGE_MESSAGE = (
    "Sorry but this action cannot run when the configuration is updated"
)


class Command(typing.NamedTuple):
    name: str
    args: str
    action: actions.Action


async def post_comment(
    ctxt: context.Context,
    message: str,
) -> None:
    try:
        await ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={"body": message},
        )
    except http.HTTPClientSideError as e:  # pragma: no cover
        ctxt.log.error(
            "fail to post comment on the pull request",
            status_code=e.status_code,
            error=e.message,
        )


@dataclasses.dataclass
class CommandInvalid(Exception):
    message: str


def load_command(
    mergify_config: rules.MergifyConfig,
    message: str,
) -> Command:
    """Load an action from a message."""
    action_classes = actions.get_commands()
    match = COMMAND_MATCHER.search(message)
    if match and match[1] in action_classes:
        action_name = match[1]
        action_class = action_classes[action_name]
        command_args = match[2].strip()

        action_config = {}
        if defaults_actions := mergify_config["defaults"].get("actions"):
            if default_action_config := defaults_actions.get(action_name):
                action_config = default_action_config

        action_config.update(action_class.command_to_config(command_args))
        try:
            action = voluptuous.Schema(
                action_class.get_schema(partial_validation=False)
            )(action_config)
        except voluptuous.Invalid:
            raise CommandInvalid(
                INVALID_COMMAND_ARGS_MESSAGE.format(command=action_name)
            )
        return Command(action_name, command_args, action)

    raise CommandInvalid(UNKNOWN_COMMAND_MESSAGE)


async def on_each_event(event: github_types.GitHubEventIssueComment) -> None:
    action_classes = actions.get_commands()
    match = COMMAND_MATCHER.search(event["comment"]["body"])
    if match and match[1] in action_classes:
        owner_login = event["repository"]["owner"]["login"]
        owner_id = event["repository"]["owner"]["id"]
        repo_name = event["repository"]["name"]
        installation_json = await github.get_installation_from_account_id(owner_id)
        async with github.aget_client(installation_json) as client:
            await client.post(
                f"/repos/{owner_login}/{repo_name}/issues/comments/{event['comment']['id']}/reactions",
                json={"content": "+1"},
                api_version="squirrel-girl",
            )


async def run_pending_commands_tasks(
    ctxt: context.Context, mergify_config: rules.MergifyConfig
) -> None:
    if ctxt.is_merge_queue_pr():
        # We don't allow any command yet
        return

    pendings = set()
    async for comment in ctxt.client.items(
        f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments"
    ):
        if comment["user"]["id"] != config.BOT_USER_ID:
            continue

        # Old format
        match = COMMAND_RESULT_MATCHER_OLD.search(comment["body"])
        if match:
            command = match[1]
            state = match[2]
            if state == "pending":
                pendings.add(command)
            elif command in pendings:
                pendings.remove(command)

            continue

        # New format
        match = COMMAND_RESULT_MATCHER.search(comment["body"])

        if match is None:
            continue

        try:
            payload = json.loads(match[1])
        except Exception:
            LOG.warning("Unable to load command payload: %s", match[1])
            continue

        command = payload.get("command")
        if not command:
            continue

        conclusion_str = payload.get("conclusion")

        try:
            conclusion = check_api.Conclusion(conclusion_str)
        except ValueError:
            LOG.error("Unable to load conclusions %s", conclusion_str)
            continue

        if conclusion == check_api.Conclusion.PENDING:
            pendings.add(command)
        elif command in pendings:
            try:
                pendings.remove(command)
            except KeyError:
                LOG.error("Unable to remove command: %s", command)

    for pending in pendings:
        await handle(ctxt, mergify_config, f"@Mergifyio {pending}", None, rerun=True)


async def run_command(
    ctxt: context.Context,
    command: Command,
    user: typing.Optional[github_types.GitHubAccount],
) -> typing.Tuple[check_api.Result, str]:

    statsd.increment("engine.commands.count", tags=[f"name:{command.name}"])

    if command.args:
        command_full = f"{command.name} {command.args}"
    else:
        command_full = command.name

    conds = conditions.PullRequestRuleConditions(
        await command.action.get_conditions_requirements(ctxt)
    )
    await conds([ctxt.pull_request])
    if conds.match:
        result = await command.action.run(
            ctxt,
            rules.EvaluatedRule(rules.PullRequestRule("", None, conds, {}, False)),
        )
    elif actions.ActionFlag.ALLOW_AS_PENDING_COMMAND in command.action.flags:
        result = check_api.Result(
            check_api.Conclusion.PENDING,
            "Waiting for conditions to match",
            conds.get_summary(),
        )
    else:
        result = check_api.Result(
            check_api.Conclusion.NEUTRAL,
            "Nothing to do",
            conds.get_summary(),
        )

    ctxt.log.info(
        "command %s: %s",
        command.name,
        result.conclusion,
        command_full=command_full,
        result=result,
        user=user["login"] if user else None,
    )
    # NOTE: Do not serialize this with Mergify JSON encoder:
    # we don't want to allow loading/unloading weird value/classes
    # this could be modified by a user, so we keep it really straightforward
    payload = {
        "command": command_full,
        "conclusion": result.conclusion.value,
    }
    details = ""
    if result.summary:
        details = f"<details>\n\n{result.summary}\n\n</details>\n"

    return (
        result,
        f"""> {command_full}

#### {result.conclusion.emoji} {result.title}

{details}

<!--
DO NOT EDIT
-*- Mergify Payload -*-
{json.dumps(payload)}
-*- Mergify Payload End -*-
-->
""",
    )


async def handle(
    ctxt: context.Context,
    mergify_config: rules.MergifyConfig,
    comment: str,
    user: typing.Optional[github_types.GitHubAccount],
    rerun: bool = False,
) -> None:
    if "@mergify " not in comment.lower() and "@mergifyio " not in comment.lower():
        return

    # Run command only if this is a pending task or if user have permission to do it.
    if not rerun and not user:
        raise RuntimeError("user must be set if rerun is false")

    def log(comment_out: str, result: typing.Optional[check_api.Result] = None) -> None:
        ctxt.log.info(
            "ran command",
            user_login=None if user is None else user["login"],
            rerun=rerun,
            comment_in=comment,
            comment_out=comment_out,
            result=result,
        )

    if "@mergifyio" in comment.lower():  # @mergify have been used instead
        footer = ""
    else:
        footer = "\n\n" + WRONG_ACCOUNT_MESSAGE

    if user:
        if (
            user["id"] != ctxt.pull["user"]["id"]
            and user["id"] != config.BOT_USER_ID
            and not await ctxt.repository.has_write_permission(user)
        ):
            message = f"@{user['login']} is not allowed to run commands"
            log(message)
            await post_comment(ctxt, message + footer)
            return

    try:
        command = load_command(mergify_config, comment)
    except CommandInvalid as e:
        log(e.message)
        await post_comment(ctxt, e.message + footer)
        return

    if (
        ctxt.configuration_changed
        and actions.ActionFlag.ALLOW_ON_CONFIGURATION_CHANGED
        not in command.action.flags
    ):
        message = CONFIGURATION_CHANGE_MESSAGE
        log(message)
        await post_comment(ctxt, message + footer)
        return

    if command.name != "refresh" and ctxt.is_merge_queue_pr():
        log(MERGE_QUEUE_COMMAND_MESSAGE)
        await post_comment(ctxt, MERGE_QUEUE_COMMAND_MESSAGE + footer)
        return

    result, message = await run_command(ctxt, command, user)
    if result.conclusion is check_api.Conclusion.PENDING and rerun:
        log("action still pending", result)
        return

    log(message, result)
    await post_comment(ctxt, message + footer)
