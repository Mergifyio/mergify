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
import enum
import itertools
import re
import typing

import daiquiri

from mergify_engine import actions
from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import context
from mergify_engine import queue
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import http


if typing.TYPE_CHECKING:
    from mergify_engine import rules

LOG = daiquiri.getLogger(__name__)

BRANCH_PROTECTION_FAQ_URL = (
    "https://docs.mergify.io/faq.html#"
    "mergify-is-unable-to-merge-my-pull-request-due-to-"
    "my-branch-protection-settings"
)

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)
REQUIRED_STATUS_RE = re.compile(r'Required status check "([^"]*)" is expected.')


class PriorityAliases(enum.Enum):
    low = 1000
    medium = 2000
    high = 3000


def Priority(v):
    try:
        return PriorityAliases[v].value
    except KeyError:
        return v


class MergeBaseAction(actions.Action):
    only_once = True

    @abc.abstractmethod
    def _should_be_queued(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    @abc.abstractmethod
    def _should_be_merged(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    @abc.abstractmethod
    def _should_be_synced(self, ctxt: context.Context, q: queue.Queue) -> bool:
        pass

    @abc.abstractmethod
    def _should_be_cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> bool:
        pass

    @abc.abstractmethod
    def get_merge_conditions(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
    ) -> typing.Tuple["rules.RuleConditions", "rules.RuleMissingConditions"]:
        pass

    def get_strict_status(
        self,
        ctxt: context.Context,
        rule: "rules.EvaluatedRule",
        q: queue.Queue,
        is_behind: bool = False,
    ) -> check_api.Result:

        summary = ""
        if self.config["strict"] in ("smart+fasttrack", "smart+ordered"):
            position = q.get_position(ctxt)
            if position is None:
                ctxt.log.error("expected queued pull request not found in queue")
                title = "The pull request is queued to be merged"
            else:
                ord = utils.to_ordinal_numeric(position + 1)
                title = f"The pull request is the {ord} in the queue to be merged"

            if is_behind:
                summary = "\nThe pull request base branch will be updated before being merged."

        elif self.config["strict"] and is_behind:
            title = "The pull request will be updated with its base branch soon"
        else:
            title = "The pull request will be merged soon"

        summary += self.get_queue_summary(ctxt, q)
        conditions, missing_conditions = self.get_merge_conditions(ctxt, rule)

        summary += "\n\nRequired conditions for merge:\n"
        for cond in conditions:
            checked = " " if cond in missing_conditions else "X"
            summary += f"\n- [{checked}] `{cond}`"

        return check_api.Result(check_api.Conclusion.PENDING, title, summary)

    def run(
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
            permission = ctxt.client.item(
                f"{ctxt.base_url}/collaborators/{self.config['merge_bot_account']}/permission"
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

        q = queue.Queue.from_context(ctxt)

        report = self.merge_report(ctxt)
        if report is not None:
            q.remove_pull(ctxt.pull["number"])
            return report

        if self.config["strict"] in ("smart+fasttrack", "smart+ordered"):
            if self._should_be_queued(ctxt, q):
                q.add_pull(ctxt, self.config)
            else:
                q.remove_pull(ctxt.pull["number"])
                return check_api.Result(
                    check_api.Conclusion.CANCELLED,
                    "The pull request have been removed from the queue",
                    "The queue conditions cannot be reach due to failing checks.",
                )

        try:
            if self._should_be_merged(ctxt, q):
                result = self._merge(ctxt, rule, q)
            elif self._should_be_synced(ctxt, q):
                result = self._sync_with_base_branch(ctxt, rule, q)
            else:
                result = self.get_strict_status(ctxt, rule, q, is_behind=ctxt.is_behind)
        except Exception:
            q.remove_pull(ctxt.pull["number"])
            raise
        if result.conclusion is not check_api.Conclusion.PENDING:
            q.remove_pull(ctxt.pull["number"])
        return result

    def cancel(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule"
    ) -> check_api.Result:
        self._set_effective_priority(ctxt)

        q = queue.Queue.from_context(ctxt)
        output = self.merge_report(ctxt)
        if output:
            q.remove_pull(ctxt.pull["number"])
            if ctxt.pull["state"] == "closed":
                return output
            else:
                return self.cancelled_check_report

        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merged if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and not self._should_be_cancel(ctxt, rule):
            try:
                if self._should_be_merged(ctxt, q):
                    # Just wait for CIs to finish
                    result = self.get_strict_status(
                        ctxt, rule, q, is_behind=ctxt.is_behind
                    )
                elif self._should_be_synced(ctxt, q):
                    # Something got merged in the base branch in the meantime: rebase it again
                    result = self._sync_with_base_branch(ctxt, rule, q)
                else:
                    result = self.get_strict_status(
                        ctxt, rule, q, is_behind=ctxt.is_behind
                    )
            except Exception:
                q.remove_pull(ctxt.pull["number"])
                raise
            if result.conclusion is not check_api.Conclusion.PENDING:
                q.remove_pull(ctxt.pull["number"])
            return result

        q.remove_pull(ctxt.pull["number"])

        return self.cancelled_check_report

    def _set_effective_priority(self, ctxt):
        if ctxt.subscription.has_feature(subscription.Features.PRIORITY_QUEUES):
            self.config["effective_priority"] = self.config["priority"]
        else:
            self.config["effective_priority"] = PriorityAliases.medium.value

    def _sync_with_base_branch(
        self, ctxt: context.Context, rule: "rules.EvaluatedRule", q: queue.Queue
    ) -> check_api.Result:
        method = self.config["strict_method"]
        user = self.config["update_bot_account"] or self.config["bot_account"]

        try:
            if method == "merge":
                branch_updater.update_with_api(ctxt)
            else:
                output = branch_updater.pre_rebase_check(ctxt)
                if output:
                    return output
                branch_updater.rebase_with_git(ctxt, user)
        except branch_updater.BranchUpdateFailure as e:
            # NOTE(sileht): Maybe the PR has been rebased and/or merged manually
            # in the meantime. So double check that to not report a wrong status.
            ctxt.update()
            output = self.merge_report(ctxt)
            if output:
                return output
            else:
                q.move_pull_at_end(ctxt.pull["number"], self.config)
                return check_api.Result(
                    check_api.Conclusion.FAILURE,
                    "Base branch update has failed",
                    e.message,
                )
        else:
            return self.get_strict_status(ctxt, rule, q, is_behind=False)

    @staticmethod
    def _get_commit_message(pull_request, mode="default"):
        if mode == "title+body":
            # Include PR number to mimic default GitHub format
            return f"{pull_request.title} (#{pull_request.number})", pull_request.body

        if not pull_request.body:
            return

        found = False
        message_lines = []

        for line in pull_request.body.split("\n"):
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
                pull_request.render_template(title.strip()),
                pull_request.render_template(
                    "\n".join(line.strip() for line in message_lines)
                ),
            )

    def _merge(
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
            commit_title_and_message = self._get_commit_message(
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
            ctxt.client.put(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/merge",
                oauth_token=oauth_token,  # type: ignore
                json=data,
            )
        except http.HTTPClientSideError as e:  # pragma: no cover
            ctxt.update()
            if ctxt.pull["merged"]:
                ctxt.log.info("merged in the meantime")
            else:
                return self._handle_merge_error(e, ctxt, rule, q)
        else:
            ctxt.update()
            ctxt.log.info("merged")

        result = self.merge_report(ctxt)
        if result:
            return result
        else:
            return check_api.Result(
                check_api.Conclusion.FAILURE,
                "Unexpected after merge pull request state",
                "The pull request have been merged, but GitHub API still report it open",
            )

    def _handle_merge_error(
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
            if self._should_be_synced(ctxt, q):
                return self._sync_with_base_branch(ctxt, rule, q)
            else:
                return self.get_strict_status(ctxt, rule, q, is_behind=ctxt.is_behind)

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

    def merge_report(self, ctxt: context.Context) -> typing.Optional[check_api.Result]:
        if ctxt.pull["draft"]:
            conclusion = check_api.Conclusion.PENDING
            title = "Draft flag needs to be removed"
            summary = ""
        elif ctxt.pull["merged"]:
            if ctxt.pull["merged_by"]["login"] in [
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
        elif ctxt.pull["mergeable_state"] == "behind" and not self.config["strict"]:
            # Strict mode has been enabled in branch protection but not in
            # mergify
            conclusion = check_api.Conclusion.FAILURE
            title = "Branch protection setting 'strict' conflicts with Mergify configuration"
            summary = ""

        elif ctxt.github_workflow_changed():
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

    def get_queue_summary(self, ctxt: context.Context, q: queue.Queue) -> str:
        pulls = q.get_pulls()
        if not pulls:
            return ""

        # NOTE(sileht): It would be better to get that from configuration, but we
        # don't have it here, so just guess it.
        priorities_configured = False

        summary = "\n\nThe following pull requests are queued:"
        for priority, grouped_pulls in itertools.groupby(
            pulls, key=lambda v: q.get_config(v)["priority"]
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
