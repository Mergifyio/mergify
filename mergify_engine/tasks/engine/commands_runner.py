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
import httpx
import voluptuous

from mergify_engine import actions
from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import mergify_pull
from mergify_engine.clients import github
from mergify_engine.worker import app


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


def spawn_pending_commands_tasks(pull, sources):
    pendings = set()
    for comment in pull.client.items(f"issues/{pull.data['number']}/comments"):
        if comment["user"]["id"] != config.BOT_USER_ID:
            return
        match = COMMAND_RESULT_MATCHER.search(comment["body"])
        if match:
            command = match[1]
            state = match[2]
            if state == "pending":
                pendings.add(command)
            elif command in pendings:
                pendings.remove(command)

    for pending in pendings:
        run_command_async.s(
            pull.installation_id,
            pull.data,
            sources,
            "@Mergifyio %s" % pending,
            None,
            rerun=True,
        ).apply_async()


@app.task
def run_command_async(
    installation_id, pull_request_raw, sources, comment, user, rerun=False
):
    owner = pull_request_raw["base"]["user"]["login"]
    repo = pull_request_raw["base"]["repo"]["name"]

    try:
        client = github.get_client(owner, repo, installation_id)
    except exceptions.MergifyNotInstalled:
        return

    pull = mergify_pull.MergifyPull(client, pull_request_raw)
    return run_command(pull, sources, comment, user, rerun)


def run_command(pull, sources, comment, user, rerun=False):
    # Run command only if this is a pending task or if user have permission to do it.
    if (
        rerun
        or user["id"] == config.BOT_USER_ID
        or pull.has_write_permissions(user["login"])
    ):
        action = load_action(comment)
        if action:
            command, command_args, method = action

            statsd.increment("engine.commands.count", tags=["name:%s" % command])

            report = method.run(pull, sources, [])

            if command_args:
                command_full = f"{command} {command_args}"
            else:
                command_full = command

            if report:
                conclusion, title, summary = report
                if conclusion is None:
                    if rerun:
                        return
                    conclusion = "pending"
                result = "**Command `{command}`: {conclusion}**\n> **{title}**\n{summary}\n".format(
                    command=command_full,
                    conclusion=conclusion,
                    title=title,
                    summary=("\n> ".join(summary.split("\n"))).strip(),
                )
            else:
                result = f"**Command `{command_full}`: success**"
        else:
            result = UNKNOWN_COMMAND_MESSAGE

        if "@mergifyio" not in comment.lower():  # @mergify have been used instead
            result += "\n\n" + WRONG_ACCOUNT_MESSAGE
    else:
        result = "@{} is not allowed to run commands".format(user["login"])

    try:
        pull.client.post(
            f"issues/{pull.data['number']}/comments", json={"body": result}
        )
    except httpx.HTTPClientSideError as e:  # pragma: no cover
        pull.log.error(
            "fail to post comment on the pull request",
            status=e.status_code,
            error=e.message,
        )
