# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
import abc
import asyncio
import enum
import itertools
import re
import typing

import daiquiri
import voluptuous

from mergify_engine import actions
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import github_types
from mergify_engine import json as mergify_json
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)

BRANCH_PROTECTION_FAQ_URL = (
    "https://docs.mergify.io/faq.html#"
    "mergify-is-unable-to-merge-my-pull-request-due-to-"
    "my-branch-protection-settings"
)

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)
REQUIRED_STATUS_RE = re.compile(r'Required status check "([^"]*)" is expected.')
FORBIDDEN_MERGE_COMMITS_MSG = "Merge commits are not allowed on this repository."
FORBIDDEN_SQUASH_MERGE_MSG = "Squash merges are not allowed on this repository."
FORBIDDEN_REBASE_MERGE_MSG = "Rebase merges are not allowed on this repository."


class PriorityAliases(enum.Enum):
    low = 1000
    medium = 2000
    high = 3000


class StrictMergeParameter(enum.Enum):
    true = True
    false = False
    fasttrack = "smart+fasttrack"
    ordered = "smart+ordered"


mergify_json.register_type(StrictMergeParameter)


def Priority(v):
    try:
        return PriorityAliases[v].value
    except KeyError:
        return v


MAX_PRIORITY: int = 10000


PrioritySchema = voluptuous.All(
    voluptuous.Any("low", "medium", "high", int),
    voluptuous.Coerce(Priority),
    int,
    voluptuous.Range(min=1, max=MAX_PRIORITY),
)


def strict_merge_parameter(v):
    if v == "smart":
        return StrictMergeParameter.ordered
    elif v == "smart+fastpath":
        return StrictMergeParameter.fasttrack
    else:
        for name, member in StrictMergeParameter.__members__.items():
            if v == member.value:
                return member

    raise ValueError(f"{v} is an unknown strict merge parameter")


class MergeBaseAction(actions.Action):
    only_once = True

    @abc.abstractmethod
    async def _should_be_queued(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    def _compute_priority(self) -> int:
        pass

    @abc.abstractmethod
    async def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    @abc.abstractmethod
    async def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    @abc.abstractmethod
    async def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        pass

    @abc.abstractmethod
    async def _get_queue(self, ctxt: context.Context) -> queue.Queue:
        pass

    @abc.abstractmethod
    def get_merge_conditions(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
    ) -> typing.Tuple["rules.RuleConditions", "rules.RuleMissingConditions"]:
        pass

    async def get_strict_status(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: queue.Queue,
        is_behind: bool = False,
    ) -> check_api.Result:

        summary = ""
        if self.config["strict"] in (
            StrictMergeParameter.fasttrack,
            StrictMergeParameter.ordered,
        ):
            position = await q.get_position(ctxt)
            if position is None:
                ctxt.log.error("expected queued pull request not found in queue")
                title = "The pull request is queued to be merged"
            else:
                ord = utils.to_ordinal_numeric(position + 1)
                title = f"The pull request is the {ord} in the queue to be merged"

            if is_behind:
                summary = "\nThe pull request base branch will be updated before being merged."

        elif self.config["strict"] is not StrictMergeParameter.false and is_behind:
            title = "The pull request will be updated with its base branch soon"
        else:
            title = "The pull request will be merged soon"

        summary += await self.get_queue_summary(ctxt, q)
        conditions, missing_conditions = self.get_merge_conditions(ctxt, rule)

        if q.train is None:
            summary += "\n\nRequired conditions for merge:\n"
        else:
            summary += "\n\nRequired conditions for queue:\n"
        for cond in conditions:
            checked = " " if cond in missing_conditions else "X"
            summary += f"\n- [{checked}] `{cond}`"

        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    async def run(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        if not config.GITHUB_APP:
            if self.config["strict_method"] == "rebase":
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Misconfigured for GitHub Action",
                    "Due to GitHub Action limitation, `strict_method: rebase` "
                    "is only available with the Mergify GitHub App",
                )

        if self.config["bot_account"] is not None:
            ctxt.log.info("legacy bot_account used")

        if self.config["update_bot_account"] and not ctxt.subscription.has_feature(
            subscription.Features.MERGE_BOT_ACCOUNT
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Merge with `update_bot_account` set are disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        if self.config["merge_bot_account"] and not ctxt.subscription.has_feature(
            subscription.Features.MERGE_BOT_ACCOUNT
        ):
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Merge with `merge_bot_account` set are disabled",
                ctxt.subscription.missing_feature_reason(
                    ctxt.pull["base"]["repo"]["owner"]["login"]
                ),
            )

        if self.config["merge_bot_account"]:
            permission = (
                await ctxt.client.item(
                    f"{ctxt.base_url}/collaborators/{self.config['merge_bot_account']}/permission"
                )
            )["permission"]
            if permission not in ("write", "maintain"):
                return check_api.Result(
                    check_api.Conclusion.ACTION_REQUIRED,
                    (
                        f"`{self.config['merge_bot_account']}` account used as "
                        "`merge_bot_account` must have `write` or `maintain` permission, "
                        f"not `{permission}`"
                    ),
                    "",
                )

        self._set_effective_priority(ctxt)

        ctxt.log.info("process merge", config=self.config)

        q = await self._get_queue(ctxt)

        report = await self.merge_report(ctxt)
        if report is not None:
            await q.remove_pull(ctxt)
            return report

        if self.config["strict"] in (
            StrictMergeParameter.fasttrack,
            StrictMergeParameter.ordered,
        ):
            if await self._should_be_queued(ctxt, q):
                await q.add_pull(ctxt, typing.cast(queue.QueueConfig, self.config))
            else:
                await q.remove_pull(ctxt)
                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    "The pull request have been removed from the queue",
                    "The queue conditions cannot be satisfied due to failing checks.",
                )

        try:
            if await self._should_be_merged(ctxt, q):
                result = await self._merge(ctxt, rule, q)
            elif await self._should_be_synced(ctxt, q):
                result = await self._sync_with_base_branch(ctxt, rule, q)
            else:
                result = await self.get_strict_status(
                    ctxt, rule, q, is_behind=await ctxt.is_behind
                )
        except Exception:
            await q.remove_pull(ctxt)
            raise
        if result.conclusion is not check_api.Conclusion.PENDING:
            await q.remove_pull(ctxt)
        return result

    async def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        self._set_effective_priority(ctxt)

        q = await self._get_queue(ctxt)
        output = await self.merge_report(ctxt)
        if output:
            await q.remove_pull(ctxt)
            if ctxt.pull["state"] == "closed":
                return output
            else:
                return self.cancelled_check_report

        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merged if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config[
            "strict"
        ] is not StrictMergeParameter.false and not await self._should_be_cancel(
            ctxt, rule
        ):
            try:
                if await self._should_be_merged(ctxt, q):
                    # Just wait for CIs to finish
                    result = await self.get_strict_status(
                        ctxt, rule, q, is_behind=await ctxt.is_behind
                    )
                elif await self._should_be_synced(ctxt, q):
                    # Something got merged in the base branch in the meantime: rebase it again
                    result = await self._sync_with_base_branch(ctxt, rule, q)
                else:
                    result = await self.get_strict_status(
                        ctxt, rule, q, is_behind=await ctxt.is_behind
                    )
            except Exception:
                await q.remove_pull(ctxt)
                raise
            if result.conclusion is not check_api.Conclusion.PENDING:
                await q.remove_pull(ctxt)
            return result

        await q.remove_pull(ctxt)

        return self.cancelled_check_report

    def _set_effective_priority(self, ctxt):
        if ctxt.subscription.has_feature(subscription.Features.PRIORITY_QUEUES):
            self.config["effective_priority"] = self._compute_priority()
        else:
            self.config["effective_priority"] = PriorityAliases.medium.value

    async def _sync_with_base_branch(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.Queue
    ) -> check_api.Result:
        method = self.config["strict_method"]
        user = self.config["update_bot_account"] or self.config["bot_account"]

        try:
            if method == "merge":
                await branch_updater.update_with_api(ctxt)
            else:
                output = branch_updater.pre_rebase_check(ctxt)
                if output:
                    return output
                await branch_updater.rebase_with_git(ctxt, user)
        except branch_updater.BranchUpdateFailure as e:
            # NOTE(sileht): Maybe the PR has been rebased and/or merged manually
            # in the meantime. So double check that to not report a wrong status.
            await ctxt.update()
            output = await self.merge_report(ctxt)
            if output:
                return output
            else:
                await q.remove_pull(ctxt)
                await q.add_pull(ctxt, typing.cast(queue.QueueConfig, self.config))
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Base branch update has failed",
                    e.message,
                )
        else:
            return await self.get_strict_status(ctxt, rule, q, is_behind=False)

    @staticmethod
    async def _get_commit_message(pull_request, mode="default"):
        body = await pull_request.body

        if mode == "title+body":
            # Include PR number to mimic default GitHub format
            return (
                f"{(await pull_request.title)} (#{(await pull_request.number)})",
                body,
            )

        if not body:
            return

        found = False
        message_lines = []

        for line in body.split("\n"):
            if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                found = True
            elif found and MARKDOWN_TITLE_RE.match(line):
                break
            elif found:
                message_lines.append(line)

        # Remove the first empty lines
        message_lines = list(
            itertools.dropwhile(lambda x: not x.strip(), message_lines)
        )

        if found and message_lines:
            title = message_lines.pop(0)

            # Remove the empty lines between title and message body
            message_lines = list(
                itertools.dropwhile(lambda x: not x.strip(), message_lines)
            )

            return (
                await pull_request.render_template(title.strip()),
                await pull_request.render_template(
                    "\n".join(line.strip() for line in message_lines)
                ),
            )

    async def _merge(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: queue.Queue,
    ) -> check_api.Result:
        if self.config["method"] != "rebase" or ctxt.pull["rebaseable"]:
            method = self.config["method"]
        elif self.config["rebase_fallback"]:
            method = self.config["rebase_fallback"]
        else:
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Automatic rebasing is not possible, manual intervention required",
                "",
            )

        data = {}

        try:
            commit_title_and_message = await self._get_commit_message(
                ctxt.pull_request,
                self.config["commit_message"],
            )
        except context.RenderTemplateFailure as rmf:
            return check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                "Invalid commit message",
                str(rmf),
            )

        if commit_title_and_message is not None:
            title, message = commit_title_and_message
            if title:
                data["commit_title"] = title
            if message:
                data["commit_message"] = message

        data["sha"] = ctxt.pull["head"]["sha"]
        data["merge_method"] = method

        bot_account = self.config["merge_bot_account"]
        if bot_account:
            oauth_token = ctxt.subscription.get_token_for(bot_account)
            if not oauth_token:
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    f"Unable to rebase: user `{bot_account}` is unknown. ",
                    f"Please make sure `{bot_account}` has logged in Mergify dashboard.",
                )
        else:
            oauth_token = None

        try:
            await ctxt.client.put(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/merge",
                oauth_token=oauth_token,  # type: ignore
                json=data,
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            await ctxt.update()
            if ctxt.pull["merged"]:
                ctxt.log.info("merged in the meantime")
            else:
                return await self._handle_merge_error(e, ctxt, rule, q)
        else:
            await ctxt.update()
            ctxt.log.info("merged")

        result = await self.merge_report(ctxt)
        if result:
            return result
        else:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Unexpected after merge pull request state",
                "The pull request have been merged, but GitHub API still report it open",
            )

    async def _handle_merge_error(
        self,
        e: http.HTTPClientSideError,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: queue.Queue,
    ) -> check_api.Result:
        if "Head branch was modified" in e.message:
            ctxt.log.info(
                "Head branch was modified in the meantime",
                status_code=e.status_code,
                error_message=e.message,
            )
            return check_api.Result(
                check_api.Conclusion.CANCELLED,
                "Head branch was modified in the meantime",
                "The head branch was modified, the merge action have been cancelled.",
            )
        elif "Base branch was modified" in e.message:
            # NOTE(sileht): The base branch was modified between pull.is_behind call and
            # here, usually by something not merged by mergify. So we need sync it again
            # with the base branch.
            ctxt.log.info(
                "Base branch was modified in the meantime, retrying",
                status_code=e.status_code,
                error_message=e.message,
            )
            # FIXME(sileht): Not sure this code handles all cases, maybe we should just
            # send a refresh to this PR to retry later
            if await self._should_be_synced(ctxt, q):
                return await self._sync_with_base_branch(ctxt, rule, q)
            else:
                return await self.get_strict_status(
                    ctxt, rule, q, is_behind=await ctxt.is_behind
                )

        elif e.status_code == 405:
            if REQUIRED_STATUS_RE.match(e.message):
                ctxt.log.info(
                    "Waiting for the branch protection required status checks to be validated",
                    status_code=e.status_code,
                    error_message=e.message,
                )
                return check_api.Result(
                    check_api.Conclusion.PENDING,
                    "Waiting for the branch protection required status checks to be validated",
                    "[Branch protection](https://docs.github.com/en/github/administering-a-repository/about-protected-branches) is enabled and is preventing Mergify "
                    "to merge the pull request. Mergify will merge when "
                    "the [required status check](https://docs.github.com/en/github/administering-a-repository/about-required-status-checks) "
                    f"validate the pull request. (detail: {e.message})",
                )
            elif FORBIDDEN_REBASE_MERGE_MSG in e.message:
                ctxt.log.info(
                    "Repository configuration doesn't allow rebase merge",
                    status_code=e.status_code,
                    error_message=e.message,
                )
                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    e.message,
                    "The repository configuration doesn't allow rebase merge. "
                    "The merge `method` configured in Mergify configuration must be "
                    "allowed in the repository configuration settings.",
                )

            elif FORBIDDEN_SQUASH_MERGE_MSG in e.message:
                ctxt.log.info(
                    "Repository configuration doesn't allow squash merge",
                    status_code=e.status_code,
                    error_message=e.message,
                )
                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    e.message,
                    "The repository configuration doesn't allow squash merge. "
                    "The merge `method` configured in Mergify configuration must be "
                    "allowed in the repository configuration settings.",
                )

            elif FORBIDDEN_MERGE_COMMITS_MSG in e.message:
                ctxt.log.info(
                    "Repository configuration doesn't allow merge commit",
                    status_code=e.status_code,
                    error_message=e.message,
                )
                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    e.message,
                    "The repository configuration doesn't allow merge commits. "
                    "The merge `method` configured in Mergify configuration must be "
                    "allowed in the repository configuration settings.",
                )

            else:
                ctxt.log.info(
                    "Branch protection settings are not validated anymore",
                    status_code=e.status_code,
                    error_message=e.message,
                )

                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    "Branch protection settings are not validated anymore",
                    "[Branch protection](https://docs.github.com/en/github/administering-a-repository/about-protected-branches) is enabled and is preventing Mergify "
                    "to merge the pull request. Mergify will merge when "
                    "branch protection settings validate the pull request once again. "
                    f"(detail: {e.message})",
                )
        else:
            message = "Mergify failed to merge the pull request"
            ctxt.log.info(
                "merge fail",
                status_code=e.status_code,
                mergify_message=message,
                error_message=e.message,
            )
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                message,
                f"GitHub error message: `{e.message}`",
            )

    async def merge_report(
        self, ctxt: context.Context
    ) -> typing.Optional[check_api.Result]:
        if ctxt.pull["draft"]:
            conclusion = check_api.Conclusion.PENDING
            title = "Draft flag needs to be removed"
            summary = ""
        elif ctxt.pull["merged"]:
            if ctxt.pull["merged_by"] is None:
                mode = "somehow"
            elif ctxt.pull["merged_by"]["login"] in [
                "mergify[bot]",
                "mergify-test[bot]",
            ]:
                mode = "automatically"
            else:
                mode = "manually"
            conclusion = check_api.Conclusion.SUCCESS
            title = "The pull request has been merged %s" % mode
            summary = "The pull request has been merged %s at *%s*" % (
                mode,
                ctxt.pull["merge_commit_sha"],
            )
        elif ctxt.pull["state"] == "closed":
            conclusion = check_api.Conclusion.CANCELLED
            title = "The pull request has been closed manually"
            summary = ""

        # NOTE(sileht): Take care of all branch protection state
        elif ctxt.pull["mergeable_state"] == "dirty":
            conclusion = check_api.Conclusion.CANCELLED
            title = "Merge conflict needs to be solved"
            summary = ""

        elif ctxt.pull["mergeable_state"] == "unknown":
            conclusion = check_api.Conclusion.FAILURE
            title = "Pull request state reported as `unknown` by GitHub"
            summary = ""
        # FIXME(sileht): We disable this check as github wrongly report
        # mergeable_state == blocked sometimes. The workaround is to try to merge
        # it and if that fail we checks for blocking state.
        # elif ctxt.pull["mergeable_state"] == "blocked":
        #     conclusion = "failure"
        #     title = "Branch protection settings are blocking automatic merging"
        #     summary = ""
        elif (
            ctxt.pull["mergeable_state"] == "behind"
            and self.config["strict"] is StrictMergeParameter.false
        ):
            # Strict mode has been enabled in branch protection but not in
            # mergify
            conclusion = check_api.Conclusion.FAILURE
            title = "Branch protection setting 'strict' conflicts with Mergify configuration"
            summary = ""

        elif await ctxt.github_workflow_changed():
            conclusion = check_api.Conclusion.ACTION_REQUIRED
            title = "Pull request must be merged manually."
            summary = """GitHub App like Mergify are not allowed to merge pull request where `.github/workflows` is changed.
<br />
This pull request must be merged manually."""

        # NOTE(sileht): remaining state "behind, clean, unstable, has_hooks
        # are OK for us
        else:
            return None

        return check_api.Result(conclusion, title, summary)

    async def get_queue_summary(self, ctxt: context.Context, q: queue.Queue) -> str:
        pulls = await q.get_pulls()
        if not pulls:
            return ""

        # NOTE(sileht): It would be better to get that from configuration, but we
        # don't have it here, so just guess it.
        priorities_configured = False

        summary = "\n\nThe following pull requests are queued:"

        async def _get_config(
            p: github_types.GitHubPullRequestNumber,
        ) -> typing.Tuple[github_types.GitHubPullRequestNumber, int]:
            return p, (await q.get_config(p))["priority"]

        pulls_priorities: typing.Dict[github_types.GitHubPullRequestNumber, int] = dict(
            await asyncio.gather(*(_get_config(p) for p in pulls))
        )

        for priority, grouped_pulls in itertools.groupby(
            pulls, key=lambda p: pulls_priorities[p]
        ):
            if priority != PriorityAliases.medium.value:
                priorities_configured = True

            try:
                fancy_priority = PriorityAliases(priority).name
            except ValueError:
                fancy_priority = str(priority)

            formatted_pulls = ", ".join((f"#{p}" for p in grouped_pulls))
            summary += f"\n* {formatted_pulls} (priority: {fancy_priority})"

        if priorities_configured and not ctxt.subscription.has_feature(
            subscription.Features.PRIORITY_QUEUES
        ):
            summary += "\n\n⚠ *Ignoring merge priority*\n"
            summary += ctxt.subscription.missing_feature_reason(
                ctxt.pull["base"]["repo"]["owner"]["login"]
            )

        return summary
