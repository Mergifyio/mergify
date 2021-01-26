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
import json
import typing
from urllib import parse

import daiquiri
import first

from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.clients import http


@dataclasses.dataclass
class TrainCarPullRequestCreationFailure(Exception):
    car: "TrainCar"


class WaitingPull(typing.NamedTuple):
    user_pull_request_number: github_types.GitHubPullRequestNumber
    queue_name: rules.QueueName


@dataclasses.dataclass
class TrainCar:
    train: "Train" = dataclasses.field(repr=False)
    user_pull_request_number: github_types.GitHubPullRequestNumber
    queue_name: rules.QueueName
    parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    current_base_sha: github_types.SHAType
    queue_pull_request_number: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = dataclasses.field(default=None)

    class Serialized(typing.TypedDict):
        user_pull_request_number: github_types.GitHubPullRequestNumber
        queue_name: rules.QueueName
        parent_pull_request_numbers: typing.List[github_types.GitHubPullRequestNumber]
        initial_current_base_sha: github_types.SHAType
        current_base_sha: github_types.SHAType
        queue_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]

    def serialized(self) -> "TrainCar.Serialized":
        return self.Serialized(
            user_pull_request_number=self.user_pull_request_number,
            queue_name=self.queue_name,
            parent_pull_request_numbers=self.parent_pull_request_numbers,
            initial_current_base_sha=self.initial_current_base_sha,
            current_base_sha=self.current_base_sha,
            queue_pull_request_number=self.queue_pull_request_number,
        )

    @classmethod
    def deserialize(cls, train: "Train", data: "TrainCar.Serialized") -> "TrainCar":
        return cls(train, **data)

    def _get_embarked_refs(self, include_my_self=True):
        pull_refs = ", ".join(
            [
                f"#{p}"
                for p in self.parent_pull_request_numbers
                + ([self.user_pull_request_number] if include_my_self else [])
            ]
        )
        return f"{self.train.ref} ({self.initial_current_base_sha[:7]}), {pull_refs}"

    async def create_pull(self) -> None:
        # TODO(sileht): reuse branch instead of recreating PRs ?

        branch_name = f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.user_pull_request_number}"

        try:
            await self.train.repository.installation.client.post(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.name}/git/refs",
                json={
                    "ref": f"refs/heads/{branch_name}",
                    "sha": self.initial_current_base_sha,
                },
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422 and "Reference already exists" in exc.message:
                escaped_branch_name = (
                    f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/"
                    f"{parse.quote(self.train.ref, safe='')}/"
                    f"{self.user_pull_request_number}"
                )
                try:
                    await self.train.repository.installation.client.delete(
                        f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.name}/git/refs/heads/{escaped_branch_name}"
                    )
                except http.HTTPClientSideError as exc_patch:
                    await self._report_failure(exc_patch)
                    raise TrainCarPullRequestCreationFailure(self) from exc_patch

            else:
                await self._report_failure(exc)
                raise TrainCarPullRequestCreationFailure(self) from exc

        for pull_number in self.parent_pull_request_numbers + [
            self.user_pull_request_number
        ]:
            # TODO(sileht): if a merge fail we should update the summary of self.user_pull_request_number with
            # the failure
            try:
                await self.train.repository.installation.client.post(
                    f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.name}/merges",
                    json={
                        "base": branch_name,
                        "head": f"refs/pull/{pull_number}/head",
                        "commit_message": f"Merge of #{pull_number}",
                    },
                )
            except http.HTTPClientSideError as e:
                await self._report_failure(e)
                raise TrainCarPullRequestCreationFailure(self) from e

        # TODO(sileht): Maybe we should handle the case the pull request already exists?
        # Since we plan to reuse pull request soon, we don't care for now
        try:
            title = f"merge-queue: embarking {self._get_embarked_refs()} together"
            body = ""
            tmp_pull = (
                await self.train.repository.installation.client.post(
                    f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.name}/pulls",
                    json={
                        "title": title,
                        "body": body,
                        "base": self.train.ref,
                        "head": branch_name,
                    },
                )
            ).json()
        except http.HTTPClientSideError as e:
            await self._report_failure(e)
            raise TrainCarPullRequestCreationFailure(self) from e

        self.queue_pull_request_number = github_types.GitHubPullRequestNumber(
            tmp_pull["number"]
        )

        await self.update_summaries(
            await self.train.repository.get_pull_request_context(
                self.queue_pull_request_number, tmp_pull
            ),
            check_api.Conclusion.PENDING,
        )

    async def delete_pull(self) -> None:
        if not self.queue_pull_request_number:
            return

        branch = f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.user_pull_request_number}"
        try:
            await self.train.repository.installation.client.delete(
                f"/repos/{self.train.repository.installation.owner_login}/{self.train.repository.name}/git/refs/heads/{branch}"
            )
        except http.HTTPNotFound:
            pass

    async def _report_failure(self, exception: http.HTTPClientSideError) -> None:
        title = "This pull request cannot be embarked for merge"
        summary = exception.message

        # Update the original Pull Request
        original_ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )
        original_ctxt.log.info(
            "pull request cannot be embarked for merge",
            status=check_api.Conclusion.ACTION_REQUIRED,
            title=title,
            summary=summary,
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

        async with utils.aredis_for_stream() as redis_stream:
            await github_events.send_refresh(
                self.train.repository.installation.redis,
                redis_stream,
                original_ctxt.pull,
            )

    async def update_summaries(
        self,
        tmp_pull_ctxt: context.Context,
        status: check_api.Conclusion,
        *,
        queue_rule: typing.Optional[rules.EvaluatedQueueRule] = None,
        will_be_reset: bool = False,
    ) -> None:
        if status == check_api.Conclusion.SUCCESS:
            title = f"The pull request #{self.user_pull_request_number} is mergeable"
        elif status == check_api.Conclusion.PENDING:
            title = f"The pull request #{self.user_pull_request_number} is embarked for merge"
        else:
            title = f"The pull request #{self.user_pull_request_number} cannot be merged and has been disembarked"

        if queue_rule:
            queue_summary = "\n\nRequired conditions for merge:\n"
            for cond in queue_rule.conditions:
                checked = " " if cond in queue_rule.missing_conditions else "X"
                queue_summary += f"\n- [{checked}] `{cond}`"
        else:
            queue_summary = ""

        summary = f"Embarking {self._get_embarked_refs()} together"
        summary += queue_summary

        await tmp_pull_ctxt.set_summary_check(
            check_api.Result(
                status,
                title=title,
                summary=summary,
            )
        )

        # Update the original Pull Request
        original_ctxt = await self.train.repository.get_pull_request_context(
            self.user_pull_request_number
        )

        if will_be_reset:
            # TODO(sileht): display train cars ?
            title = f"The pull request is going to be re-embarked soon: {tmp_pull_ctxt.pull['html_url']}"
        else:
            if status == check_api.Conclusion.SUCCESS:
                title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} is mergeable"
            elif status == check_api.Conclusion.PENDING:
                title = f"The pull request is embarked with {self._get_embarked_refs(include_my_self=False)} for merge"
            else:
                title = f"The pull request embarked with {self._get_embarked_refs(include_my_self=False)} cannot be merged and has been disembarked"

        report = check_api.Result(status, title=title, summary=queue_summary)
        original_ctxt.log.info(
            "pull request train car status update",
            conclusion=status.value,
            report=report,
        )
        await check_api.set_check_run(
            original_ctxt,
            constants.MERGE_QUEUE_SUMMARY_NAME,
            report,
        )

        async with utils.aredis_for_stream() as redis_stream:
            await github_events.send_refresh(
                self.train.repository.installation.redis,
                redis_stream,
                original_ctxt.pull,
            )


@dataclasses.dataclass
class Train:
    repository: context.Repository
    ref: github_types.GitHubRefType
    max_size: int = 5  # Allow to be configured

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

    def _get_train_queue_key(self) -> str:
        return f"merge-train~{self.repository.installation.owner_id}~{self.repository.id}~{self.ref}"

    async def load(self):
        train_raw = await self.repository.installation.redis.get(
            self._get_train_queue_key()
        )

        if train_raw:
            train = self.Serialized(json.loads(train_raw))
            self._waiting_pulls = [WaitingPull(*wp) for wp in train["waiting_pulls"]]
            self._current_base_sha = train["current_base_sha"]
            self._cars = [TrainCar.deserialize(self, c) for c in train["cars"]]

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.repository.installation.owner_login,
            gh_repo=self.repository.name,
            gh_branch=self.ref,
            max_size=self.max_size,
            train_cars=[c.user_pull_request_number for c in self._cars],
            train_waiting_pulls=[
                wp.user_pull_request_number for wp in self._waiting_pulls
            ],
        )

    async def _save(self):
        if self._waiting_pulls or self._cars:
            prepared = self.Serialized(
                waiting_pulls=self._waiting_pulls,
                current_base_sha=self._current_base_sha,
                cars=[c.serialized() for c in self._cars],
            )
            raw = json.dumps(prepared)
            await self.repository.installation.redis.set(
                self._get_train_queue_key(), raw
            )
        else:
            await self.repository.installation.redis.delete(self._get_train_queue_key())

    def get_car_by_tmp_pull(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        return first.first(
            self._cars,
            key=lambda car: car.queue_pull_request_number == ctxt.pull["number"],
        )

    async def refresh(self) -> None:
        await self._populate_cars()
        await self._save()
        self.log.info("train cars refreshed")

    async def reset(self) -> None:
        await self._slice_cars_at(0)
        await self._populate_cars()
        await self._save()
        self.log.info("train cars reset")

    async def _slice_cars_at(self, position: int) -> None:
        for c in reversed(self._cars[position:]):
            self._waiting_pulls.insert(
                0, WaitingPull(c.user_pull_request_number, c.queue_name)
            )
            await c.delete_pull()
        self._cars = self._cars[:position]

    async def insert_pull_at(
        self, ctxt: context.Context, position: int, queue_name: rules.QueueName
    ) -> None:
        ctxt.log.info("adding to train", position=position, queue_name=queue_name)
        await self._slice_cars_at(position)
        self._waiting_pulls.insert(
            position - len(self._cars), WaitingPull(ctxt.pull["number"], queue_name)
        )
        await self._populate_cars()
        await self._save()
        ctxt.log.info("added to train", position=position, queue_name=queue_name)

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
            await self._cars[0].delete_pull()
            self._cars = self._cars[1:]

            if ctxt.pull["merge_commit_sha"] is None:
                raise RuntimeError("merged pull request without merge_commit_sha set")

            self._current_base_sha = ctxt.pull["merge_commit_sha"]

            for car in self._cars:
                car.current_base_sha = self._current_base_sha

            await self._populate_cars()
            await self._save()
            ctxt.log.info("removed from train", position=0)
            return

        position = (
            [c.user_pull_request_number for c in self._cars]
            + [wp.user_pull_request_number for wp in self._waiting_pulls]
        ).index(ctxt.pull["number"])
        await self._slice_cars_at(position)
        del self._waiting_pulls[0]
        await self._populate_cars()
        await self._save()
        ctxt.log.info("removed from train", position=position)

    async def _populate_cars(self) -> None:
        if self._current_base_sha is None or not self._cars:
            self._current_base_sha = await self.get_head_sha()

        missing_cars = self.max_size - len(self._cars)

        if missing_cars < 0:
            # Too many cars
            to_delete = self._cars[missing_cars:]
            for car in to_delete:
                await car.delete_pull()
                self._waiting_pulls.append(
                    WaitingPull(car.user_pull_request_number, car.queue_name)
                )
            self._cars = self._cars[: self.max_size]

        elif missing_cars > 0 and self._waiting_pulls:
            if self._cars and self._cars[-1].queue_pull_request_number is None:
                # NOTE(sileht): the last created pull request have failed, so don't create
                # the next one, we wait it got removed from the queue
                return

            # Not enough cars
            for _ in range(missing_cars):
                try:
                    user_pull_request_number, queue_name = self._waiting_pulls.pop(0)
                except IndexError:
                    break

                parent_pull_request_numbers = [
                    car.user_pull_request_number for car in self._cars
                ]
                car = TrainCar(
                    self,
                    user_pull_request_number,
                    queue_name,
                    parent_pull_request_numbers,
                    self._current_base_sha,
                    self._current_base_sha,
                )
                self._cars.append(car)
                try:
                    await car.create_pull()
                except TrainCarPullRequestCreationFailure:
                    # NOTE(sileht): We posted failure merge-queue check-run on
                    # car.user_pull_request_number and refreshed it, so it will be removed
                    # from the train soon. We don't need to create remaining cars now.
                    # When this car will be removed the remaining one will be created
                    return

    async def get_head_sha(self) -> github_types.SHAType:
        escaped_branch_name = parse.quote(self.ref, safe="")
        return typing.cast(
            github_types.GitHubBranch,
            await self.repository.installation.client.item(
                f"repos/{self.repository.installation.owner_login}/{self.repository.name}/branches/{escaped_branch_name}"
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
