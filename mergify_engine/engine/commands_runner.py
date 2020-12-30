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
UNKNOWN_COMMAND_MESSAGE = "Sorry but I didn't understand the command."
WRONG_ACCOUNT_MESSAGE = "_Hey, I reacted but my real name is @Mergifyio_"


def post_comment(
    ctxt: context.Context,
    message: str,
) -> None:
    try:
        ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={"body": message},
        )
    except http.HTTPClientSideError as e:  # pragma: no cover
        ctxt.log.error(
            "fail to post comment on the pull request",
            status_code=e.status_code,
            error=e.message,
        )


def load_action(
    message: str,
) -> typing.Optional[typing.Tuple[str, str, actions.Action]]:
    """Load an action from a message.

    :return: A tuple with 3 values: the command name, the commands args and the action."""
    action_classes = actions.get_commands()
    match = COMMAND_MATCHER.search(message)
    if match and match[1] in action_classes:
        action_class = action_classes[match[1]]
        command_args = match[2].strip()
        config = action_class.command_to_config(command_args)
        action = voluptuous.Schema(action_class.get_schema())(config)
        return match[1], command_args, action

    return None


async def on_each_event(event: github_types.GitHubEventIssueComment) -> None:
    action = load_action(event["comment"]["body"])
    if action:
        owner = event["repository"]["owner"]["login"]
        repo = event["repository"]["name"]
        async with await github.aget_client(owner) as client:
            await client.post(
                f"/repos/{owner}/{repo}/issues/comments/{event['comment']['id']}/reactions",
                json={"content": "+1"},
                api_version="squirrel-girl",
            )  # type: ignore[call-arg]


def run_pending_commands_tasks(ctxt: context.Context) -> None:
    pendings = set()
    for comment in ctxt.client.items(
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
        handle(ctxt, "@Mergifyio %s" % pending, None, rerun=True)


def run_action(
    ctxt: context.Context,
    action: typing.Tuple[str, str, actions.Action],
    user: typing.Optional[github_types.GitHubAccount],
) -> typing.Tuple[check_api.Result, str]:
    command, command_args, method = action

    statsd.increment("engine.commands.count", tags=["name:%s" % command])

    report = method.run(
        ctxt,
        rules.EvaluatedRule(
            "", rules.RuleConditions([]), rules.RuleMissingConditions([]), {}
        ),
    )

    if command_args:
        command_full = f"{command} {command_args}"
    else:
        command_full = command

    ctxt.log.info(
        "command %s",
        report.conclusion.name.lower(),
        command_full=command_full,
        report=report,
        user=user["login"] if user else None,
    )
    message = (
        "**Command `{command}`: {conclusion}**\n> **{title}**\n{summary}\n".format(
            command=command_full,
            conclusion=report.conclusion.name.lower(),
            title=report.title,
            summary="> " + "\n> ".join(report.summary.split("\n")).strip(),
        )
    )
    return (report, message)


def handle(
    ctxt: context.Context,
    comment: str,
    user: typing.Optional[github_types.GitHubAccount],
    rerun: bool = False,
) -> None:
    # Run command only if this is a pending task or if user have permission to do it.
    if not rerun and not user:
        raise RuntimeError("user must be set if rerun is false")

    if "@mergifyio" in comment.lower():  # @mergify have been used instead
        footer = ""
    else:
        footer = "\n\n" + WRONG_ACCOUNT_MESSAGE

    if (
        user
        and user["id"] != config.BOT_USER_ID
        and not ctxt.has_write_permissions(user["login"])
    ):
        message = "@{} is not allowed to run commands".format(user["login"])
        post_comment(ctxt, message + footer)
        return

    action = load_action(comment)
    if not action:
        message = UNKNOWN_COMMAND_MESSAGE
        post_comment(ctxt, message + footer)
        return

    result, message = run_action(ctxt, action, user)
    if result.conclusion is check_api.Conclusion.PENDING and rerun:
        return

    post_comment(ctxt, message + footer)
