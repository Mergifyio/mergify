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
import typing
from urllib import parse

import daiquiri
import first

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


@dataclasses.dataclass
class TrainCarPullRequestCreationPostponed(Exception):
    car: "TrainCar"


@dataclasses.dataclass
class TrainCarPullRequestCreationFailure(Exception):
    car: "TrainCar"


class PseudoTrainCar(typing.Protocol):
    user_pull_request_number: github_types.GitHubPullRequestNumber
    config: queue.PullQueueConfig
    queued_at: datetime.datetime


class WaitingPull(typing.NamedTuple):
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
class TrainCar(PseudoTrainCar):
    train: "Train" = dataclasses.field(repr=False)
    user_pull_request_number: github_types.GitHubPullRequestNumber
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    config: queue.PullQueueConfig
    initial_current_base_sha: github_types.SHAType
    current_base_sha: github_types.SHAType
    queued_at: datetime.datetime
    state: TrainCarState = "pending"
    queue_pull_request_number: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = dataclasses.field(default=None)

    class Serialized(typing.TypedDict):
        user_pull_request_number: github_types.GitHubPullRequestNumber
        parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
        config: queue.PullQueueConfig
        initial_current_base_sha: github_types.SHAType
        current_base_sha: github_types.SHAType
        state: TrainCarState
        queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]
        queued_at: datetime.datetime

    def serialized(self) -> "TrainCar.Serialized":
        return self.Serialized(
            user_pull_request_number=self.user_pull_request_number,
            parent_pull_request_numbers=self.parent_pull_request_numbers,
            initial_current_base_sha=self.initial_current_base_sha,
            current_base_sha=self.current_base_sha,
            state=self.state,
            queue_pull_request_number=self.queue_pull_request_number,
            config=self.config,
            queued_at=self.queued_at,
        )

    @classmethod
    def deserialize(cls, train: "Train", data: "TrainCar.Serialized") -> "TrainCar":
        # NOTE(sileht): Backward compat, can be removed soon
        if "state" not in data:
            data["state"] = "created"
        if "queued_at" not in data:
            data["queued_at"] = date.utcnow()
        return cls(train, **data)

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
            refs.append(f"#{self.user_pull_request_number}")

        if len(refs) == 1:
            return refs[0]
        else:
            return f"{', '.join(refs[:-1])} and {refs[-1]}"

    async def get_pull_request_to_evaluate(self) -> context.BasePullRequest:
        ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )
        if self.state == "created" and self.queue_pull_request_number is not None:
            tmp_ctxt = await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
            return context.QueuePullRequest(ctxt, tmp_ctxt)
        elif self.state == "updated" or self.state == "failed":
            return ctxt.pull_request
        else:
            raise RuntimeError("Invalid self state")

    async def get_context_to_evaluate(self) -> typing.Optional[context.Context]:
        if self.state == "created" and self.queue_pull_request_number is not None:
            return await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number
            )
        elif self.state == "updated":
            return await self.train.repository.get_pull_request_context(
                self.user_pull_request_number
            )
        else:
            return None

    async def update_user_pull(self, queue_rule: rules.QueueRule) -> None:
        # TODO(sileht): Add support for strict method and  update_bot_account
        # TODO(sileht): rework branch_updater to be able to use it here.
        ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )
        if not await ctxt.is_behind:
            # Already done
            return

        try:
            await ctxt.client.put(
                f"{ctxt.base_url}/pulls/{ctxt.pull['number']}/update-branch",
                api_version="lydian",
                json={"expected_head_sha": ctxt.pull["head"]["sha"]},
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422:
                refreshed_pull = await ctxt.client.item(
                    f"{ctxt.base_url}/pulls/{ctxt.pull['number']}"
                )
                if refreshed_pull["head"]["sha"] != ctxt.pull["head"]["sha"]:
                    ctxt.log.info(
                        "branch updated in the meantime",
                        status_code=exc.status_code,
                        error=exc.message,
                    )
                    return
            await self._report_failure(exc.message, "update")
            raise TrainCarPullRequestCreationFailure(self) from exc

        evaluated_queue_rule = await queue_rule.get_pull_request_rule(
            ctxt, ctxt.pull_request
        )
        await self.update_summaries(
            check_api.Conclusion.PENDING,
            check_api.Conclusion.PENDING,
            evaluated_queue_rule,
        )

    async def create_pull(
        self,
        queue_rule: rules.QueueRule,
    ) -> None:
        branch_name = f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.user_pull_request_number}"

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
                    await self._report_failure(exc_patch.message)
                    raise TrainCarPullRequestCreationFailure(self) from exc_patch
            else:
                await self._report_failure(exc.message)
                raise TrainCarPullRequestCreationFailure(self) from exc

        for pull_number in self.parent_pull_request_numbers + [
            self.user_pull_request_number
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
                        gh_pull=self.user_pull_request_number,
                        error_message=e.message,
                    )
                    await self._delete_branch()
                    raise TrainCarPullRequestCreationPostponed(self) from e
                else:
                    await self._report_failure(e.message)
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
            await self._report_failure(e.message)
            raise TrainCarPullRequestCreationFailure(self) from e

        self.queue_pull_request_number = github_types.GitHubPullRequestNumber(
            tmp_pull["number"]
        )

        ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )
        tmp_ctxt = await self.train.repository.get_pull_request_context(
            self.queue_pull_request_number
        )
        evaluated_queue_rule = await queue_rule.get_pull_request_rule(
            ctxt, context.QueuePullRequest(ctxt, tmp_ctxt)
        )
        await self.update_summaries(
            check_api.Conclusion.PENDING,
            check_api.Conclusion.PENDING,
            evaluated_queue_rule,
        )

    async def generate_merge_queue_summary(
        self,
        queue_rule: typing.Union[rules.EvaluatedQueueRule, rules.QueueRule],
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

This pull request has been created by Mergify to speculatively check the mergeability of #{self.user_pull_request_number}.
You don't need to do anything. Mergify will close this pull request automatically when it is complete.
"""

        description += f"\n\n**Required conditions of queue** `{queue_rule.name}` **for merge:**\n\n"
        description += queue_rule.conditions.get_summary()

        if show_queue:
            table = [
                "| | Pull request | Queue/Priority | Speculative checks | Queued",
                "| ---: | :--- | :--- | :--- | :--- |",
            ]
            for i, pseudo_car in enumerate(self.train._iter_pseudo_cars()):
                ctxt = await self.train.repository.get_pull_request_context(
                    pseudo_car.user_pull_request_number
                )
                pull_html_url = f"{ctxt.pull['base']['repo']['html_url']}/pull/{pseudo_car.user_pull_request_number}"
                try:
                    fancy_priority = merge_base.PriorityAliases(
                        pseudo_car.config["priority"]
                    ).name
                except ValueError:
                    fancy_priority = str(pseudo_car.config["priority"])

                speculative_checks = ""
                if isinstance(pseudo_car, TrainCar):
                    if pseudo_car.state == "updated":
                        speculative_checks = f"[in place]({pull_html_url})"
                    elif pseudo_car.state == "created":
                        speculative_checks = f"#{pseudo_car.queue_pull_request_number}"

                elapsed = date.pretty_timedelta(date.utcnow() - pseudo_car.queued_at)
                table.append(
                    f"| {i + 1} "
                    f"| {ctxt.pull['title']} ([#{pseudo_car.user_pull_request_number}]({pull_html_url})) "
                    f"| {pseudo_car.config['name']}/{fancy_priority} "
                    f"| {speculative_checks} "
                    f"| {elapsed} ago "
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

    async def delete_pull(self) -> None:
        if not self.queue_pull_request_number:
            return
        await self._delete_branch()

    async def _delete_branch(self) -> None:
        escaped_branch_name = (
            f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/"
            f"{parse.quote(self.train.ref, safe='')}/"
            f"{self.user_pull_request_number}"
        )
        try:
            await self.train.repository.installation.client.delete(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.repo['name']}/git/refs/heads/{escaped_branch_name}"
            )
        except http.HTTPNotFound:
            pass

    async def _report_failure(
        self,
        details: str,
        operation: typing.Literal["created", "update"] = "created",
    ) -> None:
        title = "This pull request cannot be embarked for merge"

        if self.queue_pull_request_number is None:
            summary = f"The merge-queue pull request can't be {operation}"
        else:
            summary = f"The merge-queue pull request (#{self.queue_pull_request_number}) can't be prepared"

        summary += f"\nDetails: `{details}`"

        # Update the original Pull Request
        original_ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
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
            await utils.send_refresh(
                self.train.repository.installation.redis,
                redis_stream,
                original_ctxt.pull["base"]["repo"],
                pull_request_number=original_ctxt.pull["number"],
                action="internal",
            )

    async def update_summaries(
        self,
        conclusion: check_api.Conclusion,
        checks_conlusion: check_api.Conclusion,
        evaluated_queue_rule: rules.EvaluatedQueueRule,
        *,
        will_be_reset: bool = False,
    ) -> None:
        if conclusion == check_api.Conclusion.SUCCESS:
            tmp_pull_title = (
                f"The pull request #{self.user_pull_request_number} is mergeable"
            )
        elif conclusion == check_api.Conclusion.PENDING:
            tmp_pull_title = f"The pull request #{self.user_pull_request_number} is embarked for merge"
        else:
            tmp_pull_title = f"The pull request #{self.user_pull_request_number} cannot be merged and has been disembarked"

        queue_summary = "\n\nRequired conditions for merge:\n\n"
        queue_summary += evaluated_queue_rule.conditions.get_summary()

        original_ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )

        if self.state == "created":
            summary = f"Embarking {self._get_embarked_refs(markdown=True)} together"
            summary += queue_summary + "\n"

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
                    f"#{self.user_pull_request_number} will be removed from the queue. 🙁"
                )
                show_queue = False
            elif conclusion == check_api.Conclusion.PENDING:
                if will_be_reset:
                    headline = f"✨ Unexpected queue change. The pull request {self.user_pull_request_number} will be re-embarked soon. ✨"
                elif checks_conlusion == check_api.Conclusion.FAILURE:
                    headline = "🕵️  This combination of pull requests has failed check. Mergify is waiting for other pull requests ahead in the queue to understand which one is responsible for the failure. 🕵️"

            body = await self.generate_merge_queue_summary(
                evaluated_queue_rule,
                for_queue_pull_request=True,
                headline=headline,
                show_queue=show_queue,
            )

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
        elif self.state == "updated":
            checks = await original_ctxt.pull_check_runs
            statuses = await original_ctxt.pull_statuses
            checked_pull = self.user_pull_request_number
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

                title = ""
                if check["output"]:
                    title = check["output"]["title"]

                check_icon_url = CHECK_ASSERTS.get(
                    check["conclusion"], CHECK_ASSERTS["neutral"]
                )

                checks_copy_summary += (
                    "<tr>"
                    f'<td align="center" width="48" height="48"><img src="{check_icon_url}" width="16" height="16" /></td>'
                    f'<td align="center" width="48" height="48"><img src="{check["app"]["owner"]["avatar_url"]}&s=40" width="16" height="16" /></td>'
                    f'<td><b>{check["app"]["name"]}/{check["name"]}</b> — {title}</td>'
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
        if will_be_reset:
            # TODO(sileht): display train cars ?
            original_pull_title = "The pull request is going to be re-embarked soon"
        else:
            if conclusion == check_api.Conclusion.SUCCESS:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} is mergeable"
            elif conclusion == check_api.Conclusion.PENDING:
                original_pull_title = f"The pull request is embarked with {self._get_embarked_refs(include_my_self=False)} for merge"
            else:
                original_pull_title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} cannot be merged and has been disembarked"

        report = check_api.Result(
            conclusion,
            title=original_pull_title,
            summary=queue_summary + "\n" + checks_copy_summary,
        )
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

        if self.state != "created":
            return

        # NOTE(sileht): refresh it, so the queue action will merge it and delete the
        # tmp_pull_ctxt branch
        with utils.aredis_for_stream() as redis_stream:
            await utils.send_refresh(
                self.train.repository.installation.redis,
                redis_stream,
                original_ctxt.pull["base"]["repo"],
                pull_request_number=original_ctxt.pull["number"],
                action="internal",
            )

        if conclusion in [check_api.Conclusion.SUCCESS, check_api.Conclusion.FAILURE]:
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

    async def has_previous_car_status_succeed(self) -> bool:
        previous_car = self._get_previous_car()
        if previous_car is None:
            return True

        previous_car_ctxt = await previous_car.get_context_to_evaluate()
        if previous_car_ctxt is None:
            return False

        previous_car_check = await previous_car_ctxt.get_engine_check_run(
            constants.MERGE_QUEUE_SUMMARY_NAME
        )
        if previous_car_check is None:
            return False

        return (
            check_api.Conclusion(previous_car_check["conclusion"])
            == check_api.Conclusion.SUCCESS
        )


@dataclasses.dataclass
class Train(queue.QueueBase):

    # Stored in redis
    _cars: typing.List[TrainCar] = dataclasses.field(default_factory=list)
    _waiting_pulls: typing.List[WaitingPull] = dataclasses.field(default_factory=list)
    _current_base_sha: typing.Optional[github_types.SHAType] = dataclasses.field(
        default=None
    )

    class Serialized(typing.TypedDict):
        cars: typing.List[TrainCar.Serialized]
        waiting_pulls: typing.List[WaitingPull]
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
            self._waiting_pulls = [WaitingPull(*wp) for wp in train["waiting_pulls"]]
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
            train_cars=[c.user_pull_request_number for c in self._cars],
            train_waiting_pulls=[
                wp.user_pull_request_number for wp in self._waiting_pulls
            ],
        )

    async def _save(self) -> None:
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

    def _should_be_updated(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> bool:
        if self._cars:
            return (
                self._cars[0].user_pull_request_number == pull_number
                and len(self._cars[0].parent_pull_request_numbers) == 0
            )
        elif self._waiting_pulls:
            return self._waiting_pulls[0].user_pull_request_number == pull_number

        return False

    def get_car(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: car.user_pull_request_number == ctxt.pull["number"],
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

        await self._populate_cars(mergify_config["queue_rules"])
        await self._save()
        self.log.info("train cars refreshed")

    async def reset(self) -> None:
        await self._slice_cars_at(0)
        await self._save()
        self.log.info("train cars reset")

    async def _slice_cars_at(self, position: int) -> None:
        for c in reversed(self._cars[position:]):
            self._waiting_pulls.insert(
                0, WaitingPull(c.user_pull_request_number, c.config, c.queued_at)
            )
            await c.delete_pull()
        self._cars = self._cars[:position]

    def _iter_pseudo_cars(self) -> typing.Iterator[PseudoTrainCar]:
        for car in self._cars:
            yield car
        for wp in self._waiting_pulls:
            # NOTE(sileht): NamedTuple doesn't support multiple inheritance
            # the Protocol can't be inherited
            yield typing.cast(PseudoTrainCar, wp)

    async def add_pull(
        self, ctxt: context.Context, config: queue.PullQueueConfig
    ) -> None:
        # TODO(sileht): handle base branch change

        best_position = -1
        for position, pseudo_car in enumerate(self._iter_pseudo_cars()):
            if pseudo_car.user_pull_request_number == ctxt.pull["number"]:
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
                > pseudo_car.config["effective_priority"]
            ):
                # We found a car with lower priority
                best_position = position

        if best_position == -1:
            best_position = len(self._cars) + len(self._waiting_pulls)

        await self._slice_cars_at(best_position)
        self._waiting_pulls.insert(
            best_position - len(self._cars),
            WaitingPull(ctxt.pull["number"], config, date.utcnow()),
        )
        await self._save()
        ctxt.log.info(
            "pull request added to train",
            position=best_position,
            queue_name=config["name"],
        )

        # Refresh summary of others
        await self._refresh_pulls(
            ctxt.pull["base"]["repo"], except_pull_request=ctxt.pull["number"]
        )

    async def remove_pull(self, ctxt: context.Context) -> None:
        ctxt.log.info("removing from train")

        if (
            ctxt.pull["merged"]
            and self._cars
            and ctxt.pull["number"] == self._cars[0].user_pull_request_number
            and await self.is_synced_with_the_base_branch()
        ):
            # Head of the train was merged and the base_sha haven't changed, we can keep
            # other running cars
            deleted_car = self._cars[0]
            await deleted_car.delete_pull()
            self._cars = self._cars[1:]

            if ctxt.pull["merge_commit_sha"] is None:
                raise RuntimeError("merged pull request without merge_commit_sha set")

            self._current_base_sha = ctxt.pull["merge_commit_sha"]

            for car in self._cars:
                car.current_base_sha = self._current_base_sha

            await self._save()
            ctxt.log.info(
                "removed from train",
                position=0,
                gh_pull_speculative_check=deleted_car.queue_pull_request_number,
            )
            await self._refresh_pulls(ctxt.pull["base"]["repo"])
            return

        position = await self.get_position(ctxt)
        if position is None:
            return

        if position < len(self._cars):
            queue_pull_request_number = self._cars[position].queue_pull_request_number
        else:
            queue_pull_request_number = None

        await self._slice_cars_at(position)
        del self._waiting_pulls[position - len(self._cars)]
        await self._save()
        ctxt.log.info(
            "removed from train",
            position=position,
            gh_pull_speculative_check=queue_pull_request_number,
        )
        await self._refresh_pulls(ctxt.pull["base"]["repo"])

    async def _populate_cars(self, queue_rules: rules.QueueRules) -> None:
        try:
            head = next(self._iter_pseudo_cars())
        except StopIteration:
            return

        if self._current_base_sha is None or not self._cars:
            self._current_base_sha = await self.get_head_sha()

        speculative_checks = head.config["queue_config"]["speculative_checks"]
        missing_cars = speculative_checks - len(self._cars)

        if missing_cars < 0:
            # Too many cars
            keep = True
            to_keep: typing.List[TrainCar] = []
            to_delete: typing.List[TrainCar] = self._cars[missing_cars:]
            for car in self._cars[:speculative_checks]:
                if car.config["name"] != head.config["name"]:
                    keep = False
                if keep:
                    to_keep.append(car)
                else:
                    to_delete.append(car)
            for car in to_delete:
                await car.delete_pull()
                self._waiting_pulls.append(
                    WaitingPull(car.user_pull_request_number, car.config, car.queued_at)
                )

        elif missing_cars > 0 and self._waiting_pulls:
            if self._cars and self._cars[-1].state == "failed":
                # NOTE(sileht): the last created pull request have failed, so don't create
                # the next one, we wait it got removed from the queue
                return

            # Not enough cars
            for _ in range(missing_cars):
                if not self._waiting_pulls:
                    return

                if self._waiting_pulls[0].config["name"] != head.config["name"]:
                    # The queue change, wait first queue to be empty before processing
                    # the next queue
                    return

                user_pull_request_number, config, queued_at = self._waiting_pulls.pop(0)

                parent_pull_request_numbers = [
                    car.user_pull_request_number for car in self._cars
                ]
                car = TrainCar(
                    self,
                    user_pull_request_number,
                    parent_pull_request_numbers,
                    config,
                    self._current_base_sha,
                    self._current_base_sha,
                    queued_at,
                )
                self._cars.append(car)

                try:
                    try:
                        queue_rule = queue_rules[car.config["name"]]
                    except KeyError:
                        self.log.warning(
                            "queue_rule not found for this train car",
                            queue_rules=queue_rules,
                            queue_name=car.config["name"],
                        )
                        await car._report_failure(
                            f"queue named `{car.config['name']}` does not exists anymore"
                        )
                        raise TrainCarPullRequestCreationFailure(car)

                    if self._should_be_updated(user_pull_request_number):
                        # No need to create a pull request
                        await car.update_user_pull(queue_rule)
                        car.state = "updated"
                    else:
                        await car.create_pull(queue_rule)
                        car.state = "created"
                except TrainCarPullRequestCreationPostponed:
                    # NOTE(sileht): We can't create the tmp pull request, we will
                    # retry later. In worse case, that will be retried until the pull
                    # request become the first one in queue
                    del self._cars[-1]
                    self._waiting_pulls.insert(
                        0, WaitingPull(user_pull_request_number, config, queued_at)
                    )
                    return
                except TrainCarPullRequestCreationFailure:
                    # NOTE(sileht): We posted failure merge-queue check-run on
                    # car.user_pull_request_number and refreshed it, so it will be removed
                    # from the train soon. We don't need to create remaining cars now.
                    # When this car will be removed the remaining one will be created
                    car.state = "failed"
                    return

    async def get_head_sha(self) -> github_types.SHAType:
        escaped_branch_name = parse.quote(self.ref, safe="")
        return typing.cast(
            github_types.GitHubBranch,
            await self.repository.installation.client.item(
                f"repos/{self.repository.installation.owner_login}/{self.repository.repo['name']}/branches/{escaped_branch_name}"
            ),
        )["commit"]["sha"]

    async def is_synced_with_the_base_branch(self) -> bool:
        if not self._cars:
            return True

        head_sha = await self.get_head_sha()
        if head_sha == self._current_base_sha:
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
            f"{self.repository.base_url}/pulls/{self._cars[0].user_pull_request_number}"
        )
        return pull["merged"] and pull["merge_commit_sha"] == head_sha

    async def get_config(
        self, pull_number: github_types.GitHubPullRequestNumber
    ) -> queue.PullQueueConfig:
        item = first.first(
            self._iter_pseudo_cars(),
            key=lambda c: c.user_pull_request_number == pull_number,
        )
        if item is not None:
            return item.config

        raise RuntimeError("get_config on unknown pull request")

    async def get_pulls(self) -> typing.List[github_types.GitHubPullRequestNumber]:
        return [item.user_pull_request_number for item in self._iter_pseudo_cars()]

    async def is_first_pull(self, ctxt: context.Context) -> bool:
        item = first.first(self._iter_pseudo_cars())
        return item is not None and item.user_pull_request_number == ctxt.pull["number"]
