# -*- encoding: utf-8 -*-
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


LOG = daiquiri.getLogger(__name__)

COMMAND_MATCHER = re.compile(r"@Mergify(?:|io) (\w*)(.*)", re.IGNORECASE)
COMMAND_RESULT_MATCHER = re.compile(r"\*Command `([^`]*)`: (pending|success|failure)\*")

MERGE_QUEUE_COMMAND_MESSAGE = "Command not allowed on merge queue pull request."
UNKNOWN_COMMAND_MESSAGE = "Sorry but I didn't understand the command. Please consult [the commands documentation](https://docs.mergify.io/commands.html) \U0001F4DA."
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


def load_command(
    mergify_config: rules.MergifyConfig,
    message: str,
) -> typing.Optional[Command]:
    """Load an action from a message.

    :return: A tuple with 3 values: the command name, the commands args and the action."""
    action_classes = actions.get_commands()
    match = COMMAND_MATCHER.search(message)
    if match and match[1] in action_classes:
        action_name = match[1]
        action_class = action_classes[action_name]
        command_args = match[2].strip()

        action_config = {}
        if defaults := mergify_config["raw"].get("defaults"):
            if defaults_actions := defaults.get("actions"):
                if default_action_config := defaults_actions.get(action_name):
                    action_config = default_action_config

        action_config.update(action_class.command_to_config(command_args))
        action = voluptuous.Schema(action_class.get_schema(partial_validation=False))(
            action_config
        )
        return Command(action_name, command_args, action)

    return None


async def on_each_event(event: github_types.GitHubEventIssueComment) -> None:
    action_classes = actions.get_commands()
    match = COMMAND_MATCHER.search(event["comment"]["body"])
    if match and match[1] in action_classes:
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        async with github.aget_client(owner) as client:
            await client.post(
                f"/repos/{owner}/{repo}/issues/comments/{event['comment']['id']}/reactions",
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
        match = COMMAND_RESULT_MATCHER.search(comment["body"])
        if match:
            command = match[1]
            state = match[2]
            if state == "pending":
                pendings.add(command)
            elif command in pendings:
                pendings.remove(command)

    for pending in pendings:
        await handle(ctxt, mergify_config, f"@Mergifyio {pending}", None, rerun=True)


async def run_command(
    ctxt: context.Context,
    command: Command,
    user: typing.Optional[github_types.GitHubAccount],
) -> typing.Tuple[check_api.Result, str]:

    statsd.increment("engine.commands.count", tags=[f"name:{command.name}"])

    report = await command.action.run(
        ctxt,
        rules.EvaluatedRule(rules.Rule("", None, rules.RuleConditions([]), {}, False)),
    )

    if command.args:
        command_full = f"{command.name} {command.args}"
    else:
        command_full = command.name

    conclusion = report.conclusion.name.lower()
    summary = "> " + "\n> ".join(report.summary.split("\n")).strip()

    ctxt.log.info(
        "command %s",
        conclusion,
        command_full=command_full,
        report=report,
        user=user["login"] if user else None,
    )
    return (
        report,
        f"**Command `{command_full}`: {conclusion}**\n> **{report.title}**\n{summary}\n",
    )


async def handle(
    ctxt: context.Context,
    mergify_config: rules.MergifyConfig,
    comment: str,
    user: typing.Optional[github_types.GitHubAccount],
    rerun: bool = False,
) -> None:
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

    command = load_command(mergify_config, comment)
    if not command:
        message = UNKNOWN_COMMAND_MESSAGE
        log(message)
        await post_comment(ctxt, message + footer)
        return

    if (
        ctxt.configuration_changed
        and not command.action.can_be_used_on_configuration_change
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
