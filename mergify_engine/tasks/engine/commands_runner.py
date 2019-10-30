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

import github

import voluptuous

from mergify_engine import actions
from mergify_engine import config
from mergify_engine import mergify_pull
from mergify_engine import utils
from mergify_engine.worker import app

LOG = daiquiri.getLogger(__name__)

COMMAND_MATCHER = re.compile(r"@Mergify(?:|io) (\w*)(.*)", re.IGNORECASE)
COMMAND_RESULT_MATCHER = re.compile(r"\*Command `([^`]*)`: (pending|success|failure)\*")

UNKNOWN_COMMAND_MESSAGE = "Sorry but we didn't understand the command."
WRONG_ACCOUNT_MESSAGE = "_Hey, we reacted but our real name is @Mergifyio_"


def load_action(message):
    action_classes = actions.get_classes()
    match = COMMAND_MATCHER.search(message)
    if match and match[1] in action_classes:
        action_class = action_classes[match[1]]
        try:
            config = action_class.command_to_config(match[2].strip())
        except NotImplementedError:
            return

        action = voluptuous.Schema(action_class.get_schema())(config)
        command = "%s%s" % (match[1], match[2])
        return command, action


def spawn_pending_commands_tasks(installation_id, event_type, data, g_pull):
    pendings = set()
    for comment in g_pull.get_issue_comments():
        if comment.user.id != config.BOT_USER_ID:
            return
        match = COMMAND_RESULT_MATCHER.search(comment.body)
        if match:
            command = match[1]
            state = match[2]
            if state == "pending":
                pendings.add(command)
            elif command in pendings:
                pendings.remove(command)

    for pending in pendings:
        run_command.s(
            installation_id, event_type, data, "@mergifyio %s" % pending, rerun=True
        ).apply_async()


@app.task
def run_command(installation_id, event_type, data, comment, rerun=False):
    installation_token = utils.get_installation_token(installation_id)
    if not installation_token:
        return

    pull = mergify_pull.MergifyPull.from_raw(
        installation_id, installation_token, data["pull_request"]
    )

    # Run command only if this is a pending task or if user have permission to do it.
    if (
        rerun
        or data["comment"]["user"]["id"] == config.BOT_USER_ID
        or pull.g_pull.base.repo.get_collaborator_permission(
            data["comment"]["user"]["login"]
        )
        in ["admin", "write"]
    ):
        action = load_action(comment)
        if action:
            command, method = action

            report = method.run(
                installation_id, installation_token, event_type, data, pull, []
            )

            if report:
                conclusion, title, summary = report
                if conclusion is None:
                    if rerun:
                        return
                    conclusion = "pending"
                result = "**Command `{command}`: {conclusion}**\n> **{title}**\n{summary}\n".format(
                    command=command,
                    conclusion=conclusion,
                    title=title,
                    summary=("\n> ".join(summary.split("\n"))).strip(),
                )
            else:
                result = "**Command `{}`: success**".format(command)
        else:
            result = UNKNOWN_COMMAND_MESSAGE

        if "@mergifyio" not in comment:  # @mergify have been used instead
            result += "\n\n" + WRONG_ACCOUNT_MESSAGE

    else:
        result = "@{} is not allowed to run commands".format(
            data["comment"]["user"]["login"]
        )

    try:
        pull.g_pull.create_issue_comment(result)
    except github.GithubException as e:  # pragma: no cover
        LOG.error(
            "fail to post comment on the pull request",
            status=e.status,
            error=e.data["message"],
            pull_request=pull,
        )
