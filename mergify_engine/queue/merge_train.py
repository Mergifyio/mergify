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

import dataclasses
import datetime
import itertools
import typing
from urllib import parse

import daiquiri
import first

from mergify_engine import branch_updater
from mergify_engine import check_api
from mergify_engine import config
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import json
from mergify_engine import queue
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.actions import merge_base
from mergify_engine.clients import http


CHECK_ASSERTS = {
    # green check mark
    "success": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/check-green-16.png",
    # red x
    "failure": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "error": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "cancelled": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "skipped": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
    "action_required": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    "timed_out": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/x-red-16.png",
    # yellow dot
    "pending": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/dot-yellow-16.png",
    None: "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/dot-yellow-16.png",
    # grey square
    "neutral": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
    "stale": "https://raw.githubusercontent.com/Mergifyio/mergify-engine/master/assets/square-grey-16.png",
}


class UnexpectedChange:
    pass


@dataclasses.dataclass
class UnexpectedDraftPullRequestChange(UnexpectedChange):
    draft_pull_request_number: github_types.GitHubPullRequestNumber

    def __str__(self) -> str:
        return f"the draft pull request #{self.draft_pull_request_number} has been manually updated"


@dataclasses.dataclass
class UnexpectedUpdatedPullRequestChange(UnexpectedChange):
    updated_pull_request_number: github_types.GitHubPullRequestNumber

    def __str__(self) -> str:
        return f"the updated pull request #{self.updated_pull_request_number} has been manually updated"


@dataclasses.dataclass
class UnexpectedBaseBranchChange(UnexpectedChange):
    base_sha: github_types.SHAType

    def __str__(self) -> str:
        return f"an external action moved the branch head {self.base_sha}"


@dataclasses.dataclass
class TrainCarPullRequestCreationPostponed(Exception):
    car: "TrainCar"


@dataclasses.dataclass
class TrainCarPullRequestCreationFailure(Exception):
    car: "TrainCar"


class EmbarkedPullWithCar(typing.NamedTuple):
    embarked_pull: "EmbarkedPull"
    car: typing.Optional["TrainCar"]


class EmbarkedPull(typing.NamedTuple):
    user_pull_request_number: github_types.GitHubPullRequestNumber
    config: queue.PullQueueConfig
    queued_at: datetime.datetime


TrainCarState = typing.Literal[
    "pending",
    "created",
    "updated",
    "failed",
]


@dataclasses.dataclass
class TrainCar:
    train: "Train" = dataclasses.field(repr=False)
    initial_embarked_pulls: typing.List[EmbarkedPull]
    still_queued_embarked_pulls: typing.List[EmbarkedPull]
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    creation_state: TrainCarState = "pending"
    checks_conclusion: check_api.Conclusion = check_api.Conclusion.PENDING
    queue_pull_request_number: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = dataclasses.field(default=None)
    failure_history: typing.List["TrainCar"] = dataclasses.field(
        default_factory=list, repr=False
    )
    head_branch: typing.Optional[str] = None

    class Serialized(typing.TypedDict):
        initial_embarked_pulls: typing.List[EmbarkedPull]
        still_queued_embarked_pulls: typing.List[EmbarkedPull]
        parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
        initial_current_base_sha: github_types.SHAType
        checks_conclusion: check_api.Conclusion
        creation_state: TrainCarState
        queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]
        # mymy can't parse recursive definition, yet
        failure_history: typing.List["TrainCar.Serialized"]  # type: ignore[misc]
        head_branch: typing.Optional[str]

    def serialized(self) -> "TrainCar.Serialized":
        return self.Serialized(
            initial_embarked_pulls=self.initial_embarked_pulls,
            still_queued_embarked_pulls=self.still_queued_embarked_pulls,
            parent_pull_request_numbers=self.parent_pull_request_numbers,
            initial_current_base_sha=self.initial_current_base_sha,
            creation_state=self.creation_state,
            checks_conclusion=self.checks_conclusion,
            queue_pull_request_number=self.queue_pull_request_number,
            failure_history=[fh.serialized() for fh in self.failure_history],
            head_branch=self.head_branch,
        )

    @classmethod
    def deserialize(
        cls,
        train: "Train",
        data: "TrainCar.Serialized",
    ) -> "TrainCar":
        if "initial_embarked_pulls" in data:
            initial_embarked_pulls = [
                EmbarkedPull(*ep) for ep in data["initial_embarked_pulls"]
            ]
            still_queued_embarked_pulls = [
                EmbarkedPull(*ep) for ep in data["still_queued_embarked_pulls"]
            ]

        else:
            # old format
            initial_embarked_pulls = [
                EmbarkedPull(
                    data["user_pull_request_number"],  # type: ignore
                    data["config"],  # type: ignore[typeddict-item]
                    data["queued_at"],  # type: ignore[typeddict-item]
                )
            ]
            still_queued_embarked_pulls = initial_embarked_pulls.copy()

        if "creation_state" in data:
            creation_state = data["creation_state"]
        else:
            creation_state = data["state"]  # type: ignore[typeddict-item]

        if "failure_history" in data:
            failure_history = [
                TrainCar.deserialize(train, fh) for fh in data["failure_history"]
            ]
        else:
            failure_history = []

        car = cls(
            train,
            initial_embarked_pulls=initial_embarked_pulls,
            still_queued_embarked_pulls=still_queued_embarked_pulls,
            parent_pull_request_numbers=data["parent_pull_request_numbers"],
            initial_current_base_sha=data["initial_current_base_sha"],
            creation_state=creation_state,
            checks_conclusion=data.get(
                "checks_conclusion", check_api.Conclusion.PENDING
            ),
            queue_pull_request_number=data["queue_pull_request_number"],
            failure_history=failure_history,
            head_branch=data.get("head_branch"),
        )
        if "head_branch" not in data:
            car.head_branch = car._get_pulls_branch_ref()
        return car

    def _get_user_refs(self) -> str:
        refs = [f"#{ep.user_pull_request_number}" for ep in self.initial_embarked_pulls]
        if len(refs) == 1:
            return refs[0]
        else:
            return f"[{' + '.join(refs)}]"

    def _get_embarked_refs(
        self, include_my_self: bool = True, markdown: bool = False
    ) -> str:
        if markdown:
            refs = [
                f"Branch **{self.train.ref}** ({self.initial_current_base_sha[:7]})"
            ]
        else:
            refs = [f"{self.train.ref} ({self.initial_current_base_sha[:7]})"]

        refs += [f"#{p}" for p in self.parent_pull_request_numbers]

        if include_my_self:
            return f"{', '.join(refs)} and {self._get_user_refs()}"
        elif len(refs) == 1:
            return refs[-1]
        else:
            return f"{', '.join(refs[:-1])} and {refs[-1]}"

    async def get_pull_requests_to_evaluate(
        self,
    ) -> typing.List[context.BasePullRequest]:
        if self.creation_state == "updated":
            if len(self.still_queued_embarked_pulls) != 1:
                raise RuntimeError("multiple embarked_pulls but state==updated")
            ctxt = await self.train.repository.get_pull_request_context(
                self.still_queued_embarked_pulls[0].user_pull_request_number
            )
            return [ctxt.pull_request]
        elif self.creation_state == "created":
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is created, but queue_pull_request_number is None"
                )

            tmp_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
            return [
                context.QueuePullRequest(
                    await self.train.repository.get_pull_request_context(
                        ep.user_pull_request_number
                    ),
                    tmp_ctxt,
                )
                for ep in self.still_queued_embarked_pulls
            ]
        elif self.creation_state == "failed":
            # Will be splitted or dropped soon
            return [
                (
                    await self.train.repository.get_pull_request_context(
                        ep.user_pull_request_number
                    )
                ).pull_request
                for ep in self.still_queued_embarked_pulls
            ]
        else:
            raise RuntimeError(f"Invalid state: {self.creation_state}")

    async def get_context_to_evaluate(self) -> typing.Optional[context.Context]:
        if (
            self.creation_state == "created"
            and self.queue_pull_request_number is not None
        ):
            return await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
        elif self.creation_state == "updated":
            if len(self.still_queued_embarked_pulls) != 1:
                raise RuntimeError("multiple embarked_pulls but state==updated")
            return await self.train.repository.get_pull_request_context(
                self.still_queued_embarked_pulls[0].user_pull_request_number
            )
        else:
            return None

    async def is_behind(self) -> bool:
        ctxt = await self.train.repository.get_pull_request_context(
            self.still_queued_embarked_pulls[0].user_pull_request_number
        )
        return await ctxt.is_behind

    async def update_user_pull(self, queue_rule: rules.QueueRule) -> None:
        if len(self.still_queued_embarked_pulls) != 1:
            raise RuntimeError("multiple embarked_pulls but state==updated")

        self.creation_state = "updated"

        ctxt = await self.train.repository.get_pull_request_context(
            self.still_queued_embarked_pulls[0].user_pull_request_number
        )
        if not await ctxt.is_behind:
            # Already done, just refresh it to merge it
            with utils.aredis_for_stream() as redis_stream:
                await utils.send_pull_refresh(
                    self.train.repository.installation.redis,
                    redis_stream,
                    ctxt.pull["base"]["repo"],
                    pull_request_number=ctxt.pull["number"],
                    action="internal",
                    source="updated pull need to be merge",
                )
            return

        try:
            # TODO(sileht): fallback to "merge" and None until all configs has
            # the new fields
            await branch_updater.update(
                self.still_queued_embarked_pulls[0].config.get(
                    "update_method", "merge"
                ),
                ctxt,
                self.still_queued_embarked_pulls[0].config.get("update_bot_account"),
            )
        except branch_updater.BranchUpdateFailure as exc:
            await self._set_creation_failure(f"{exc.title}\n\n{exc.message}", "update")
            raise TrainCarPullRequestCreationFailure(self) from exc

        evaluated_queue_rule = await queue_rule.get_pull_request_rule(
            self.train.repository, self.train.ref, [ctxt.pull_request]
        )
        await self.update_summaries(
            check_api.Conclusion.PENDING,
            check_api.Conclusion.PENDING,
            evaluated_queue_rule,
        )

    def _get_pulls_branch_ref(self) -> str:
        return "-".join(
            [str(ep.user_pull_request_number) for ep in self.initial_embarked_pulls]
        )

    async def create_pull(
        self,
        queue_rule: rules.QueueRule,
    ) -> None:

        self.head_branch = self._get_pulls_branch_ref()

        branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.head_branch}"
        )

        self.creation_state = "created"

        try:
            await self.train.repository.installation.client.post(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/git/refs",
                json={
                    "ref": f"refs/heads/{branch_name}",
                    "sha": self.initial_current_base_sha,
                },
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422 and "Reference already exists" in exc.message:
                try:
                    await self._delete_branch()
                except http.HTTPClientSideError as exc_patch:
                    await self._set_creation_failure(exc_patch.message)
                    raise TrainCarPullRequestCreationFailure(self) from exc_patch
            else:
                await self._set_creation_failure(exc.message)
                raise TrainCarPullRequestCreationFailure(self) from exc

        for pull_number in self.parent_pull_request_numbers + [
            ep.user_pull_request_number for ep in self.still_queued_embarked_pulls
        ]:
            try:
                await self.train.repository.installation.client.post(
                    f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/merges",
                    json={
                        "base": branch_name,
                        "head": f"refs/pull/{pull_number}/head",
                        "commit_message": f"Merge of #{pull_number}",
                    },
                )
            except http.HTTPClientSideError as e:
                if (
                    e.status_code == 403
                    and "Resource not accessible by integration" in e.message
                ):
                    self.train.log.info(
                        "fail to create the queue pull request due to GitHub App restriction",
                        embarked_pulls=[
                            ep.user_pull_request_number
                            for ep in self.still_queued_embarked_pulls
                        ],
                        error_message=e.message,
                    )
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationPostponed(self) from e
                else:
                    await self._set_creation_failure(e.message)
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationFailure(self) from e

        try:
            title = f"merge-queue: embarking {self._get_embarked_refs()} together"
            body = await self.generate_merge_queue_summary(
                queue_rule, for_queue_pull_request=True
            )
            tmp_pull = (
                await self.train.repository.installation.client.post(
                    f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/pulls",
                    json={
                        "title": title,
                        "body": body,
                        "base": self.train.ref,
                        "head": branch_name,
                        "draft": True,
                    },
                )
            ).json()
        except http.HTTPClientSideError as e:
            await self._set_creation_failure(e.message)
            raise TrainCarPullRequestCreationFailure(self) from e

        self.queue_pull_request_number = github_types.GitHubPullRequestNumber(
            tmp_pull["number"]
        )

        queue_pull_requests = await self.get_pull_requests_to_evaluate()
        evaluated_queue_rule = await queue_rule.get_pull_request_rule(
            self.train.repository, self.train.ref, queue_pull_requests
        )
        await self.update_summaries(
            check_api.Conclusion.PENDING,
            check_api.Conclusion.PENDING,
            evaluated_queue_rule,
        )

    async def generate_merge_queue_summary(
        self,
        queue_rule: typing.Optional[
            typing.Union[rules.EvaluatedQueueRule, rules.QueueRule]
        ],
        *,
        for_queue_pull_request: bool = False,
        show_queue: bool = True,
        headline: typing.Optional[str] = None,
    ) -> str:
        description = ""
        if headline:
            description += f"**{headline}**\n\n"

        description += (
            f"{self._get_embarked_refs(markdown=True)} are embarked together for merge."
        )

        if for_queue_pull_request:
            description += f"""

This pull request has been created by Mergify to speculatively check the mergeability of {self._get_user_refs()}.
You don't need to do anything. Mergify will close this pull request automatically when it is complete.
"""

        if queue_rule is not None:
            description += f"\n\n**Required conditions of queue** `{queue_rule.name}` **for merge:**\n\n"
            description += queue_rule.conditions.get_summary()

        if show_queue:
            table = [
                "| | Pull request | Queue/Priority | Speculative checks | Queued",
                "| ---: | :--- | :--- | :--- | :--- |",
            ]
            for i, (embarked_pull, car) in enumerate(self.train._iter_embarked_pulls()):
                ctxt = await self.train.repository.get_pull_request_context(
                    embarked_pull.user_pull_request_number
                )
                pull_html_url = f"{ctxt.pull['base']['repo']['html_url']}/pull/{embarked_pull.user_pull_request_number}"
                try:
                    fancy_priority = merge_base.PriorityAliases(
                        embarked_pull.config["priority"]
                    ).name
                except ValueError:
                    fancy_priority = str(embarked_pull.config["priority"])

                speculative_checks = ""
                if car is not None:
                    if car.creation_state == "updated":
                        speculative_checks = f"[in place]({pull_html_url})"
                    elif car.creation_state == "created":
                        speculative_checks = f"#{car.queue_pull_request_number}"

                queued_at = date.pretty_datetime(embarked_pull.queued_at)
                table.append(
                    f"| {i + 1} "
                    f"| {ctxt.pull['title']} ([#{embarked_pull.user_pull_request_number}]({pull_html_url})) "
                    f"| {embarked_pull.config['name']}/{fancy_priority} "
                    f"| {speculative_checks} "
                    f"| {queued_at}"
                    "|"
                )

            description += (
                "\n**The following pull requests are queued:**\n"
                + "\n".join(table)
                + "\n"
            )

        description += "\n---\n\n"
        description += constants.MERGIFY_MERGE_QUEUE_PULL_REQUEST_DOC
        return description.strip()

    async def delete_pull(self, reason: typing.Optional[str]) -> None:
        if not self.queue_pull_request_number or self.head_branch is None:
            return

        if self.creation_state == "created" and reason is not None:
            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is created, but queue_pull_request_number is None"
                )

            tmp_pull_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
            check = await tmp_pull_ctxt.get_engine_check_run(
                context.Context.SUMMARY_NAME
            )
            if (
                check is None
                or check["conclusion"] == check_api.Conclusion.PENDING.value
            ):
                reason = f"✨ {reason}. The pull request {self._get_user_refs()} has been re-embarked. ✨"
                body = await self.generate_merge_queue_summary(
                    None,
                    for_queue_pull_request=True,
                    headline=reason,
                    show_queue=False,
                )

                if tmp_pull_ctxt.pull["body"] != body:
                    await tmp_pull_ctxt.client.patch(
                        f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                        json={"body": body},
                    )

                await tmp_pull_ctxt.set_summary_check(
                    check_api.Result(
                        check_api.Conclusion.CANCELLED,
                        title=f"The pull request {self._get_user_refs()} has been re-embarked for merge",
                        summary=reason,
                    )
                )
                tmp_pull_ctxt.log.info("train car deleted", reason=reason)
        await self._delete_branch()

    async def _delete_branch(self) -> None:
        escaped_branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/"
            f"{parse.quote(self.train.ref, safe='')}/"
            f"{self.head_branch}"
        )
        try:
            await self.train.repository.installation.client.delete(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/git/refs/heads/{escaped_branch_name}"
            )
        except http.HTTPNotFound:
            pass
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422 and "Reference does not exist" in exc.message:
                pass
            else:
                raise

    async def _set_creation_failure(
        self,
        details: str,
        operation: typing.Literal["created", "update"] = "created",
    ) -> None:
        self.creation_state = "failed"

        title = "This pull request cannot be embarked for merge"

        if self.queue_pull_request_number is None:
            summary = f"The merge-queue pull request can't be {operation}"
        else:
            summary = f"The merge-queue pull request (#{self.queue_pull_request_number}) can't be prepared"

        summary += f"\nDetails: `{details}`"

        # Update the original Pull Request
        for embarked_pull in self.still_queued_embarked_pulls:
            original_ctxt = await self.train.repository.get_pull_request_context(
                embarked_pull.user_pull_request_number
            )
            original_ctxt.log.info(
                "pull request cannot be embarked for merge",
                conclusion=check_api.Conclusion.ACTION_REQUIRED,
                title=title,
                summary=summary,
                details=details,
                exc_info=True,
            )
            await check_api.set_check_run(
                original_ctxt,
                constants.MERGE_QUEUE_SUMMARY_NAME,
                check_api.Result(
                    check_api.Conclusion.ACTION_REQUIRED,
                    title=title,
                    summary=summary,
                ),
            )

            with utils.aredis_for_stream() as redis_stream:
                await utils.send_pull_refresh(
                    self.train.repository.installation.redis,
                    redis_stream,
                    original_ctxt.pull["base"]["repo"],
                    pull_request_number=original_ctxt.pull["number"],
                    action="internal",
                    source="draft pull creation error",
                )

    async def update_summaries(
        self,
        conclusion: check_api.Conclusion,
        checks_conclusion: check_api.Conclusion,
        evaluated_queue_rule: rules.EvaluatedQueueRule,
        *,
        unexpected_change: typing.Optional[UnexpectedChange] = None,
    ) -> None:
        self.checks_conclusion = checks_conclusion

        refs = self._get_user_refs()
        if conclusion == check_api.Conclusion.SUCCESS:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = f"The pull request {refs} is mergeable"
            else:
                tmp_pull_title = f"The pull requests {refs} are mergeable"
        elif conclusion == check_api.Conclusion.PENDING:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = f"The pull request {refs} is embarked for merge"
            else:
                tmp_pull_title = f"The pull requests {refs} are embarked for merge"
        else:
            if len(self.initial_embarked_pulls) == 1:
                tmp_pull_title = (
                    f"The pull request {refs} cannot be merged and has been disembarked"
                )
            else:
                tmp_pull_title = (
                    f"The pull requests {refs} cannot be merged and will be split"
                )

        queue_summary = "\n\nRequired conditions for merge:\n\n"
        queue_summary += evaluated_queue_rule.conditions.get_summary()

        if self.failure_history:
            batch_failure_summary = f"\n\nThe pull request {self._get_user_refs()} is part of a speculative checks batch that previously failed:\n"
            batch_failure_summary += (
                "| Pull request | Parents pull requests | Speculative checks |\n"
            )
            batch_failure_summary += "| ---: | :--- | :--- |\n"
            for failure in self.failure_history:
                if failure.creation_state == "updated":
                    speculative_checks = "[in place]"
                elif failure.creation_state == "created":
                    speculative_checks = f"#{failure.queue_pull_request_number}"
                else:
                    speculative_checks = ""
            batch_failure_summary += f"| {self._get_user_refs()} | {self._get_embarked_refs(include_my_self=False)} | {speculative_checks} |"
        else:
            batch_failure_summary = ""

        original_ctxts = [
            await self.train.repository.get_pull_request_context(
                ep.user_pull_request_number
            )
            for ep in self.still_queued_embarked_pulls
        ]

        if self.creation_state == "created":
            summary = f"Embarking {self._get_embarked_refs(markdown=True)} together"
            summary += queue_summary + "\n" + batch_failure_summary

            if self.queue_pull_request_number is None:
                raise RuntimeError(
                    "car state is created, but queue_pull_request_number is None"
                )

            tmp_pull_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )

            headline: typing.Optional[str] = None
            show_queue = True
            if conclusion == check_api.Conclusion.SUCCESS:
                headline = "🎉 This combination of pull requests has been checked successfully 🎉"
                show_queue = False
            elif conclusion == check_api.Conclusion.FAILURE:
                headline = (
                    "🙁 This combination of pull requests has failed checks. "
                    f"{self._get_user_refs()} will be removed from the queue. 🙁"
                )
                show_queue = False
            elif conclusion == check_api.Conclusion.PENDING:
                if unexpected_change is not None:
                    headline = f"✨ Unexpected queue change: {unexpected_change}. The pull request {self._get_user_refs()} will be re-embarked soon. ✨"
                elif checks_conclusion == check_api.Conclusion.FAILURE:
                    if self.has_previous_car_status_succeeded():
                        headline = "🕵️  This combination of pull requests has failed checks. Mergify will split this batch to understand which pull request is responsible for the failure. 🕵️"
                    else:
                        headline = "🕵️ This combination of pull requests has failed checks. Mergify is waiting for other pull requests ahead in the queue to understand which one is responsible for the failure. 🕵️"

            body = await self.generate_merge_queue_summary(
                evaluated_queue_rule,
                for_queue_pull_request=True,
                headline=headline,
                show_queue=show_queue,
            )

            if tmp_pull_ctxt.pull["body"] != body:
                await tmp_pull_ctxt.client.patch(
                    f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                    json={"body": body},
                )

            await tmp_pull_ctxt.set_summary_check(
                check_api.Result(
                    conclusion,
                    title=tmp_pull_title,
                    summary=summary,
                )
            )

            checks = await tmp_pull_ctxt.pull_check_runs
            statuses = await tmp_pull_ctxt.pull_statuses
            checked_pull = self.queue_pull_request_number
        elif self.creation_state == "updated":
            if len(self.still_queued_embarked_pulls) != 1:
                raise RuntimeError("multiple embarked_pulls but state==updated")
            checks = await original_ctxts[0].pull_check_runs
            statuses = await original_ctxts[0].pull_statuses
            checked_pull = self.still_queued_embarked_pulls[0].user_pull_request_number
        else:
            checks = []
            statuses = []
            checked_pull = github_types.GitHubPullRequestNumber(0)

        if checks or statuses:
            checks_copy_summary = (
                "\n\nCheck-runs and statuses of the embarked "
                f"pull request #{checked_pull}:\n\n<table>"
            )
            for check in checks:
                # Don't copy Summary/Rule/Queue/... checks
                if check["app"]["id"] == config.INTEGRATION_ID:
                    continue

                output_title = ""
                if check["output"] and check["output"]["title"]:
                    output_title = f" — {check['output']['title']}"

                check_icon_url = CHECK_ASSERTS.get(
                    check["conclusion"], CHECK_ASSERTS["neutral"]
                )

                checks_copy_summary += (
                    "<tr>"
                    f'<td align="center" width="48" height="48"><img src="{check_icon_url}" width="16" height="16" /></td>'
                    f'<td align="center" width="48" height="48"><img src="{check["app"]["owner"]["avatar_url"]}&s=40" width="16" height="16" /></td>'
                    f'<td><b>{check["app"]["name"]}/{check["name"]}</b>{output_title}</td>'
                    f'<td><a href="{check["html_url"]}">details</a></td>'
                    "</tr>"
                )

            for status in statuses:
                status_icon_url = CHECK_ASSERTS[status["state"]]

                checks_copy_summary += (
                    "<tr>"
                    f'<td align="center" width="48" height="48"><img src="{status_icon_url}" width="16" height="16" /></td>'
                    f'<td align="center" width="48" height="48"><img src="{status["avatar_url"]}&s=40" width="16" height="16" /></td>'
                    f'<td><b>{status["context"]}</b> — {status["description"]}</td>'
                    f'<td><a href="{status["target_url"]}">details</a></td>'
                    "</tr>"
                )
            checks_copy_summary += "</table>\n"
        else:
            checks_copy_summary = ""

        # Update the original Pull Request
        unexpected_change_summary = ""
        if unexpected_change is None:
            if conclusion == check_api.Conclusion.SUCCESS:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} is mergeable"
            elif conclusion == check_api.Conclusion.PENDING:
                original_pull_title = f"The pull request is embarked with {self._get_embarked_refs(include_my_self=False)} for merge"
            else:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} cannot be merged and has been disembarked"
        else:
            original_pull_title = "The pull request is going to be re-embarked soon"
            unexpected_change_summary = (
                f"✨ Unexpected queue change: {unexpected_change}. ✨\n\n"
            )

        report = check_api.Result(
            conclusion,
            title=original_pull_title,
            summary=unexpected_change_summary
            + queue_summary
            + "\n"
            + checks_copy_summary
            + "\n"
            + batch_failure_summary,
        )
        for original_ctxt in original_ctxts:
            original_ctxt.log.info(
                "pull request train car status update",
                conclusion=conclusion.value,
                report=report,
            )
            await check_api.set_check_run(
                original_ctxt,
                constants.MERGE_QUEUE_SUMMARY_NAME,
                report,
            )

            if (
                self.creation_state == "created"
                and conclusion != check_api.Conclusion.PENDING
            ):
                # NOTE(sileht): refresh it, so the queue action will merge it and delete the
                # tmp_pull_ctxt branch
                with utils.aredis_for_stream() as redis_stream:
                    await utils.send_pull_refresh(
                        self.train.repository.installation.redis,
                        redis_stream,
                        original_ctxt.pull["base"]["repo"],
                        pull_request_number=original_ctxt.pull["number"],
                        action="internal",
                        source="draft pull request state change",
                    )

        if self.creation_state != "created":
            return

        if conclusion in [
            check_api.Conclusion.SUCCESS,
            check_api.Conclusion.FAILURE,
        ]:
            await tmp_pull_ctxt.client.post(
                f"{tmp_pull_ctxt.base_url}/issues/{self.queue_pull_request_number}/comments",
                json={"body": tmp_pull_title},
            )
            await tmp_pull_ctxt.client.patch(
                f"{tmp_pull_ctxt.base_url}/pulls/{self.queue_pull_request_number}",
                json={"state": "closed"},
            )

    def _get_previous_car(self) -> typing.Optional["TrainCar"]:
        position = self.train._cars.index(self)
        if position == 0:
            return None
        else:
            return self.train._cars[position - 1]

    def has_previous_car_status_succeeded(self) -> bool:
        position = self.train._cars.index(self)
        if position == 0:
            return True
        return all(
            c.checks_conclusion == check_api.Conclusion.SUCCESS
            for c in self.train._cars[:position]
        )


@dataclasses.dataclass
class Train(queue.QueueBase):

    # Stored in redis
    _cars: typing.List[TrainCar] = dataclasses.field(default_factory=list)
    _waiting_pulls: typing.List[EmbarkedPull] = dataclasses.field(default_factory=list)
    _current_base_sha: typing.Optional[github_types.SHAType] = dataclasses.field(
        default=None
    )

    class Serialized(typing.TypedDict):
        cars: typing.List[TrainCar.Serialized]
        waiting_pulls: typing.List[EmbarkedPull]
        current_base_sha: typing.Optional[github_types.SHAType]

    @classmethod
    async def from_context(cls, ctxt: context.Context) -> "Train":
        q = await super().from_context(ctxt)
        await q.load()
        return q

    @staticmethod
    def get_redis_key_for(
        owner_id: github_types.GitHubAccountIdType,
        repository_id: typing.Union[
            github_types.GitHubRepositoryIdType, typing.Literal["*"]
        ],
        ref: typing.Union[github_types.GitHubRefType, typing.Literal["*"]],
    ) -> str:
        return f"merge-train~{owner_id}~{repository_id}~{ref}"

    def _get_redis_key(self) -> str:
        return self.get_redis_key_for(
            self.repository.installation.owner_id, self.repository.repo["id"], self.ref
        )

    @classmethod
    async def iter_trains(
        cls, installation: context.Installation
    ) -> typing.AsyncIterator["Train"]:
        async for train_name in installation.redis.scan_iter(
            cls.get_redis_key_for(installation.owner_id, "*", "*"), count=10000
        ):
            train_name_split = train_name.split("~")
            repo_id = github_types.GitHubRepositoryIdType(int(train_name_split[2]))
            ref = github_types.GitHubRefType(train_name_split[3])
            repository = await installation.get_repository_by_id(repo_id)
            yield cls(repository, ref)

    async def load(self) -> None:
        train_raw = await self.repository.installation.redis.get(self._get_redis_key())

        if train_raw:
            train = typing.cast(Train.Serialized, json.loads(train_raw))
            self._waiting_pulls = [EmbarkedPull(*wp) for wp in train["waiting_pulls"]]
            self._current_base_sha = train["current_base_sha"]
            self._cars = [TrainCar.deserialize(self, c) for c in train["cars"]]
        else:
            self._cars = []
            self._waiting_pulls = []
            self._current_base_sha = None

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.repo["name"],
            gh_branch=self.ref,
            train_cars=[
                [ep.user_pull_request_number for ep in c.still_queued_embarked_pulls]
                for c in self._cars
            ],
            train_waiting_pulls=[
                wp.user_pull_request_number for wp in self._waiting_pulls
            ],
        )

    async def save(self) -> None:
        if self._waiting_pulls or self._cars:
            prepared = self.Serialized(
                waiting_pulls=self._waiting_pulls,
                current_base_sha=self._current_base_sha,
                cars=[c.serialized() for c in self._cars],
            )
            raw = json.dumps(prepared)
            await self.repository.installation.redis.set(self._get_redis_key(), raw)
        else:
            await self.repository.installation.redis.delete(self._get_redis_key())

    def get_car(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: ctxt.pull["number"]
            in [ep.user_pull_request_number for ep in car.still_queued_embarked_pulls],
        )

    def get_car_by_tmp_pull(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: car.queue_pull_request_number == ctxt.pull["number"],
        )

    async def refresh(self) -> None:
        config_file = await self.repository.get_mergify_config_file()
        if config_file is None:
            self.log.warning(
                "train can't be refreshed, the mergify configuration is missing",
            )
            return
        try:
            mergify_config = rules.get_mergify_config(config_file)
        except rules.InvalidRules as e:  # pragma: no cover
            self.log.warning(
                "train can't be refreshed, the mergify configuration is invalid",
                summary=str(e),
                annotations=e.get_annotations(e.filename),
            )
            return

        queue_rules = mergify_config["queue_rules"]

        # NOTE(sileht): workaround for cleaning unwanted PRs queued by this bug:
        # https://github.com/Mergifyio/mergify-engine/pull/2958
        await self._remove_duplicate_pulls()
        await self._sync_configuration_change(queue_rules)
        await self._split_failed_batches(queue_rules)
        await self._populate_cars(queue_rules)
        await self.save()

    async def _remove_duplicate_pulls(self) -> None:
        known_prs = set()
        i = 0
        for car in self._cars:
            for embarked_pull in car.still_queued_embarked_pulls:
                if embarked_pull.user_pull_request_number in known_prs:
                    await self._slice_cars(
                        i, reason="The pull request has been queued twice"
                    )
                    break
                else:
                    known_prs.add(embarked_pull.user_pull_request_number)
                i += 1

        wp_to_keep = []
        for wp in self._waiting_pulls:
            if wp.user_pull_request_number not in known_prs:
                known_prs.add(wp.user_pull_request_number)
                wp_to_keep.append(wp)
        self._waiting_pulls = wp_to_keep

    async def _sync_configuration_change(self, queue_rules: rules.QueueRules) -> None:
        for i, (embarked_pull, _) in enumerate(list(self._iter_embarked_pulls())):
            queue_rule = queue_rules.get(embarked_pull.config["name"])
            if queue_rule is None:
                # NOTE(sileht): We just slice the cars list here, so when the
                # car will be recreated if the rule doesn't exists anymore, the
                # failure will be reported properly
                await self._slice_cars(
                    i, reason="The associated queue rule does not exist anymore"
                )

    async def reset(self, unexpected_change: UnexpectedChange) -> None:
        await self._slice_cars(
            0, reason=f"Unexpected queue change: {unexpected_change}."
        )
        await self.save()
        self.log.info("train cars reset")

    async def _slice_cars(self, new_queue_size: int, reason: str) -> None:
        sliced = False
        new_cars: typing.List[TrainCar] = []
        new_waiting_pulls: typing.List[EmbarkedPull] = []
        for c in self._cars:
            new_queue_size -= len(c.still_queued_embarked_pulls)
            if new_queue_size >= 0:
                new_cars.append(c)
            else:
                sliced = True
                new_waiting_pulls.extend(c.still_queued_embarked_pulls)
                await c.delete_pull(reason)

        if sliced:
            self.log.info(
                "queue has been sliced", new_queue_size=new_queue_size, reason=reason
            )

        self._cars = new_cars
        self._waiting_pulls = new_waiting_pulls + self._waiting_pulls

    def _iter_embarked_pulls(
        self,
    ) -> typing.Iterator[EmbarkedPullWithCar]:
        for car in self._cars:
            for embarked_pull in car.still_queued_embarked_pulls:
                yield EmbarkedPullWithCar(embarked_pull, car)
        for embarked_pull in self._waiting_pulls:
            # NOTE(sileht): NamedTuple doesn't support multiple inheritance
            # the Protocol can't be inherited
            yield EmbarkedPullWithCar(embarked_pull, None)

    async def add_pull(
        self, ctxt: context.Context, config: queue.PullQueueConfig
    ) -> None:
        # TODO(sileht): handle base branch change

        best_position = -1
        for position, (embarked_pull, _) in enumerate(self._iter_embarked_pulls()):
            if embarked_pull.user_pull_request_number == ctxt.pull["number"]:
                # already in queue, we are good
                self.log.info(
                    "pull request already in train",
                    gh_pull=ctxt.pull["number"],
                    config=config,
                )
                return

            if (
                best_position == -1
                and config["effective_priority"]
                > embarked_pull.config["effective_priority"]
            ):
                # We found a car with lower priorit
                best_position = position

        new_embarked_pull = EmbarkedPull(ctxt.pull["number"], config, date.utcnow())

        if best_position == -1:
            self._waiting_pulls.append(new_embarked_pull)
        else:
            await self._slice_cars(
                best_position,
                reason="Pull request with higher priority has been queued",
            )
            number_of_pulls_in_cars = sum(
                len(c.still_queued_embarked_pulls) for c in self._cars
            )
            self._waiting_pulls.insert(
                best_position - number_of_pulls_in_cars, new_embarked_pull
            )
        await self.save()
        ctxt.log.info(
            "pull request added to train",
            position=best_position,
            queue_name=config["name"],
        )

        # Refresh summary of others
        await self._refresh_pulls(
            ctxt.pull["base"]["repo"],
            source="pull added to queue",
            except_pull_request=ctxt.pull["number"],
        )

    async def remove_pull(self, ctxt: context.Context) -> None:
        ctxt.log.info("removing from train")

        if (
            ctxt.pull["merged"]
            and self._cars
            and ctxt.pull["number"]
            == self._cars[0].still_queued_embarked_pulls[0].user_pull_request_number
            and await self.is_synced_with_the_base_branch(await self.get_base_sha())
        ):
            # Head of the train was merged and the base_sha haven't changed, we can keep
            # other running cars
            del self._cars[0].still_queued_embarked_pulls[0]
            if len(self._cars[0].still_queued_embarked_pulls) == 0:
                deleted_car = self._cars[0]
                await deleted_car.delete_pull(reason=None)
                self._cars = self._cars[1:]

            if ctxt.pull["merge_commit_sha"] is None:
                raise RuntimeError("merged pull request without merge_commit_sha set")

            self._current_base_sha = ctxt.pull["merge_commit_sha"]

            await self.save()
            ctxt.log.info("removed from head train", position=0)
            await self._refresh_pulls(
                ctxt.pull["base"]["repo"],
                source="pull removed from queue",
            )
            return

        position = await self.get_position(ctxt)
        if position is None:
            return
        await self._slice_cars(
            position, reason="Pull request ahead in queue got removed from the queue"
        )
        number_of_pulls_in_cars = sum(
            len(c.still_queued_embarked_pulls) for c in self._cars
        )
        del self._waiting_pulls[position - number_of_pulls_in_cars]
        await self.save()
        ctxt.log.info("removed from train", position=position)
        await self._refresh_pulls(
            ctxt.pull["base"]["repo"], source="pull removed from queue"
        )

    async def _split_failed_batches(self, queue_rules: rules.QueueRules) -> None:
        current_queue_position = 0
        if (
            len(self._cars) == 1
            and self._cars[0].checks_conclusion == check_api.Conclusion.FAILURE
            and len(self._cars[0].initial_embarked_pulls) == 1
        ):
            # A earlier batch failed and it was the fault of the last PR of the batch
            # we refresh the draft PR, so it will set the final state
            if self._cars[0].queue_pull_request_number is not None:
                with utils.aredis_for_stream() as redis_stream:
                    await utils.send_pull_refresh(
                        self.repository.installation.redis,
                        redis_stream,
                        self.repository.repo,
                        pull_request_number=self._cars[0].queue_pull_request_number,
                        action="internal",
                        source="batch failed due to last pull",
                    )
            return

        for car in self._cars:
            current_queue_position += len(car.still_queued_embarked_pulls)
            if (
                car.checks_conclusion == check_api.Conclusion.FAILURE
                and car.has_previous_car_status_succeeded()
                and len(car.initial_embarked_pulls) > 1
            ):
                self.log.info(
                    "spliting failed car", position=current_queue_position, car=car
                )

                queue_name = car.still_queued_embarked_pulls[0].config["name"]
                try:
                    queue_rule = queue_rules[queue_name]
                except KeyError:
                    # We just need to wait the pull request has been removed from
                    # the queue by the action
                    self.log.info(
                        "cant split failed batch TrainCar, queue rule does not exist anymore",
                        queue_rules=queue_rules,
                        queue_name=queue_name,
                    )
                    return

                # NOTE(sileht): This batch failed, we can drop everything else
                # after has we known now they will not work, and split this one
                # in two
                await self._slice_cars(
                    current_queue_position,
                    reason="Pull request ahead in queue failed to get merged",
                )

                # We move this car later at the end to not retest it
                del self._cars[-1]

                parents: typing.List[EmbarkedPull] = []
                for pulls in utils.split_list(
                    car.still_queued_embarked_pulls[:-1],
                    queue_rule.config["speculative_checks"],
                ):
                    self._cars.append(
                        TrainCar(
                            train=self,
                            initial_embarked_pulls=pulls,
                            still_queued_embarked_pulls=pulls.copy(),
                            parent_pull_request_numbers=car.parent_pull_request_numbers
                            + [ep.user_pull_request_number for ep in parents],
                            initial_current_base_sha=car.initial_current_base_sha,
                            failure_history=car.failure_history + [car],
                        )
                    )

                    parents += pulls
                    try:
                        await self._create_car(queue_rule, self._cars[-1])
                    except (
                        TrainCarPullRequestCreationPostponed,
                        TrainCarPullRequestCreationFailure,
                    ):
                        self.log.info(
                            "failed to create draft pull request",
                            car=car,
                            exc_info=True,
                        )

                # Update the car to pull that was part of the batch into parent, but keep
                # the result as we already test it.
                car.parent_pull_request_numbers = car.parent_pull_request_numbers + [
                    ep.user_pull_request_number for ep in parents
                ]
                car.still_queued_embarked_pulls = [car.still_queued_embarked_pulls[-1]]
                car.initial_embarked_pulls = car.still_queued_embarked_pulls.copy()
                self._cars.append(car)

                # Refresh summary of others
                await self._refresh_pulls(
                    self.repository.repo, source="batch got split"
                )
                break

    async def _populate_cars(self, queue_rules: rules.QueueRules) -> None:
        if self._cars and (
            self._cars[-1].creation_state == "failed"
            or self._cars[-1].checks_conclusion == check_api.Conclusion.FAILURE
        ):
            # We are searching the responsible of a failure don't touch anything
            return

        try:
            head = next(self._iter_embarked_pulls()).embarked_pull
        except StopIteration:
            return

        if self._current_base_sha is None or not self._cars:
            self._current_base_sha = await self.get_base_sha()

        try:
            queue_rule = queue_rules[head.config["name"]]
        except KeyError:
            # We just need to wait the pull request has been removed from
            # the queue by the action
            self.log.info(
                "cant populate cars, queue rule does not exist",
                queue_rules=queue_rules,
                queue_name=head.config["name"],
            )
            car = TrainCar(self, [head], [head], [], self._current_base_sha)
            await car._set_creation_failure(
                f"queue named `{head.config['name']}` does not exist anymore"
            )
            return

        speculative_checks = queue_rule.config["speculative_checks"]
        missing_cars = speculative_checks - len(self._cars)

        if missing_cars < 0:
            # Too many cars
            new_queue_size = sum(
                [
                    len(car.still_queued_embarked_pulls)
                    for car in self._cars[:speculative_checks]
                ]
            )
            await self._slice_cars(
                new_queue_size,
                reason="The number of speculative checks has been reduced",
            )

        elif missing_cars > 0 and self._waiting_pulls:
            # Not enough cars
            for _ in range(missing_cars):
                pulls_to_check, self._waiting_pulls = self._get_next_batch(
                    self._waiting_pulls,
                    head.config["name"],
                    queue_rule.config["batch_size"],
                )
                if not pulls_to_check:
                    return

                # NOTE(sileht): still_queued_embarked_pulls is always in sync with self._current_base_sha.
                # A TrainCar can be partially deleted and the next car may looks wierd as some parent PRs
                # may look missing but because the current_base_sha as moved too, this is safe.
                parent_pull_request_numbers = [
                    ep.user_pull_request_number
                    for ep in itertools.chain.from_iterable(
                        [car.still_queued_embarked_pulls for car in self._cars]
                    )
                ]

                car = TrainCar(
                    self,
                    pulls_to_check,
                    pulls_to_check.copy(),
                    parent_pull_request_numbers,
                    self._current_base_sha,
                )

                self._cars.append(car)

                try:
                    await self._create_car(queue_rule, car)
                except TrainCarPullRequestCreationPostponed:
                    return
                except TrainCarPullRequestCreationFailure:
                    # NOTE(sileht): We posted failure merge-queue check-run on
                    # car.user_pull_request_number and refreshed it, so it will be removed
                    # from the train soon. We don't need to create remaining cars now.
                    # When this car will be removed the remaining one will be created
                    return

    async def _create_car(
        self,
        queue_rule: rules.QueueRule,
        car: TrainCar,
    ) -> None:
        can_be_updated = (
            self._cars[0] == car
            and len(car.still_queued_embarked_pulls) == 1
            and len(car.parent_pull_request_numbers) == 0
        )
        if can_be_updated:
            if queue_rule.config["allow_inplace_speculative_checks"]:
                must_be_updated = True
            else:
                # The pull request is already up2date no need to create
                # a pull request
                must_be_updated = not await car.is_behind()
        else:
            must_be_updated = False

        try:
            # get_next_batch() ensure all embarked_pulls has same config
            if must_be_updated:
                # No need to create a pull request
                await car.update_user_pull(queue_rule)
            else:
                await car.create_pull(queue_rule)

        except TrainCarPullRequestCreationPostponed:
            # NOTE(sileht): We can't create the tmp pull request, we will
            # retry later. In worse case, that will be retried until the pull
            # request become the first one in queue
            del self._cars[-1]
            self._waiting_pulls.extend(car.still_queued_embarked_pulls)
            raise

    async def get_base_sha(self) -> github_types.SHAType:
        escaped_branch_name = parse.quote(self.ref, safe="")
        return typing.cast(
            github_types.GitHubBranch,
            await self.repository.installation.client.item(
                f"repos/{self.repository.installation.owner_login}/{self.repository.repo['name']}/branches/{escaped_branch_name}"
            ),
        )["commit"]["sha"]

    async def is_synced_with_the_base_branch(
        self, base_sha: github_types.SHAType
    ) -> bool:
        if not self._cars:
            return True

        if base_sha == self._current_base_sha:
            return True

        if not self._cars:
            # NOTE(sileht): the PR that call this method will be deleted soon
            return False

        # Base branch just moved but the last merged PR is the one we have on top on our
        # train, we just not yet received the event that have called Train.remove_pull()
        # NOTE(sileht): I wonder if it's robust enough, these cases should be enough to
        # catch everything I have in mind
        # * We run it when we remove the top car
        # * We run it when a tmp PR is refreshed
        # * We run it on each push events
        pull: github_types.GitHubPullRequest = await self.repository.installation.client.item(
            f"{self.repository.base_url}/pulls/{self._cars[0].still_queued_embarked_pulls[0].user_pull_request_number}"
        )
        return pull["merged"] and pull["merge_commit_sha"] == base_sha

    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> queue.PullQueueConfig:
        item = first.first(
            self._iter_embarked_pulls(),
            key=lambda c: c.embarked_pull.user_pull_request_number == pull_number,
        )
        if item is not None:
            return item.embarked_pull.config

        raise RuntimeError("get_config on unknown pull request")

    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [
            item.embarked_pull.user_pull_request_number
            for item in self._iter_embarked_pulls()
        ]

    async def is_first_pull(self, ctxt: context.Context) -> bool:
        item = first.first(self._iter_embarked_pulls())
        return (
            item is not None
            and item.embarked_pull.user_pull_request_number == ctxt.pull["number"]
        )

    @staticmethod
    def _get_next_batch(
        pulls: typing.List[EmbarkedPull], queue_name: str, batch_size: int = 1
    ) -> typing.Tuple[typing.List[EmbarkedPull], typing.List[EmbarkedPull]]:
        if not pulls:
            return [], []

        for _i, pull in enumerate(pulls[:batch_size]):
            if pull.config["name"] != queue_name:
                # The queue change, wait first queue to be empty before processing
                # the next queue
                break
        else:
            _i += 1
        return pulls[:_i], pulls[_i:]
