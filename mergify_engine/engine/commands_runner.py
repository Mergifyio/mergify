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

import daiquiri
from datadog import statsd
import voluptuous

from mergify_engine import actions
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine.clients import github
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

COMMAND_MATCHER = re.compile(r"@Mergify(?:|io) (\w*)(.*)", re.IGNORECASE)
COMMAND_RESULT_MATCHER = re.compile(r"\*Command `([^`]*)`: (pending|success|failure)\*")

UNKNOWN_COMMAND_MESSAGE = "Sorry but I didn't understand the command."
WRONG_ACCOUNT_MESSAGE = "_Hey, I reacted but my real name is @Mergifyio_"


def load_action(message):
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


async def on_each_event(owner, repo, event_type, data):
    if event_type == "issue_comment":
        action = load_action(data["comment"]["body"])
        if action:
            async with await github.aget_client(owner) as client:
                await client.post(
                    f"/repos/{owner}/{repo}/issues/comments/{data['comment']['id']}/reactions",
                    json={"content": "+1"},
                    api_version="squirrel-girl",
                )


def run_pending_commands_tasks(ctxt):
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


def handle(ctxt, comment, user, rerun=False):
    # Run command only if this is a pending task or if user have permission to do it.
    if (
        rerun
        or user["id"] == config.BOT_USER_ID
        or ctxt.has_write_permissions(user["login"])
    ):
        action = load_action(comment)
        if action:
            command, command_args, method = action

            statsd.increment("engine.commands.count", tags=["name:%s" % command])

            report = method.run(ctxt, None, [])

            if command_args:
                command_full = f"{command} {command_args}"
            else:
                command_full = command

            if report:
                if report.conclusion is check_api.Conclusion.PENDING and rerun:
                    return
                result = "**Command `{command}`: {conclusion}**\n> **{title}**\n{summary}\n".format(
                    command=command_full,
                    conclusion=report.conclusion.name.lower(),
                    title=report.title,
                    summary="> " + "\n> ".join(report.summary.split("\n")).strip(),
                )
                ctxt.log.info(
                    "command %s",
                    report.conclusion.name.lower(),
                    command_full=command_full,
                    report=report,
                    user=user["login"] if user else None,
                )
            else:
                result = f"**Command `{command_full}`: success**"
                ctxt.log.info(
                    "command success",
                    command_full=command_full,
                    report=check_api.Result(
                        check_api.Conclusion.SUCCESS.value,
                        f"`{command_full}` succeed" "",
                    ),
                    user=user["login"] if user else None,
                )
        else:
            result = UNKNOWN_COMMAND_MESSAGE

        if "@mergifyio" not in comment.lower():  # @mergify have been used instead
            result += "\n\n" + WRONG_ACCOUNT_MESSAGE
    else:
        result = "@{} is not allowed to run commands".format(user["login"])

    try:
        ctxt.client.post(
            f"{ctxt.base_url}/issues/{ctxt.pull['number']}/comments",
            json={"body": result},
        )
    except http.HTTPClientSideError as e:  # pragma: no cover
        ctxt.log.error(
            "fail to post comment on the pull request",
            status=e.status_code,
            error=e.message,
        )
