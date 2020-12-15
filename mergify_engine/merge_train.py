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
import redis

from mergify_engine import check_api
from mergify_engine import constants
from mergify_engine import context
from mergify_engine import github_events
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import subscription
from mergify_engine import utils
from mergify_engine.clients import github
from mergify_engine.clients import http


@dataclasses.dataclass
class TrainCarPullRequestCreationFailure(Exception):
    car: "TrainCar"


class WaitingPull(typing.NamedTuple):
    pull_request_number: github_types.GitHubPullRequestNumber
    queue_name: rules.QueueName


@dataclasses.dataclass
class TrainCar:
    train: "Train" = dataclasses.field(repr=False)
    pull_request_number: github_types.GitHubPullRequestNumber
    queue_name: rules.QueueName
    parents: typing.List[github_types.GitHubPullRequestNumber]
    initial_current_base_sha: github_types.SHAType
    current_base_sha: github_types.SHAType
    tmp_pull_request_number: typing.Optional[
        github_types.GitHubPullRequestNumber
    ] = dataclasses.field(default=None)

    class Serialized(typing.TypedDict):
        pull_request_number: github_types.GitHubPullRequestNumber
        queue_name: rules.QueueName
        parents: typing.List[github_types.GitHubPullRequestNumber]
        initial_current_base_sha: github_types.SHAType
        current_base_sha: github_types.SHAType
        tmp_pull_request_number: typing.Optional[github_types.GitHubPullRequestNumber]

    def serialized(self) -> "TrainCar.Serialized":
        return self.Serialized(
            pull_request_number=self.pull_request_number,
            queue_name=self.queue_name,
            parents=self.parents,
            initial_current_base_sha=self.initial_current_base_sha,
            current_base_sha=self.current_base_sha,
            tmp_pull_request_number=self.tmp_pull_request_number,
        )

    @classmethod
    def deserialized(cls, train: "Train", data: "TrainCar.Serialized") -> "TrainCar":
        return cls(train, **data)

    def _get_branch_name(self, *, escaped=False):
        if escaped:
            ref = parse.quote(self.train.ref, safe="")
        else:
            ref = self.train.ref
        return f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{ref}/{self.pull_request_number}"

    def create_pull(self) -> None:
        # TODO(sileht): reuse branch instead of recreating PRs ?

        branch_name = self._get_branch_name()

        try:
            self.train.client.post(
                f"/repos/{self.train.owner_login}/{self.train.repo_name}/git/refs",
                json={
                    "ref": f"refs/heads/{branch_name}",
                    "sha": self.initial_current_base_sha,
                },
            )
        except http.HTTPClientSideError as exc:
            if exc.status_code == 422 and "Reference already exists" in exc.message:
                escaped_branch_name = self._get_branch_name(escaped=True)
                try:
                    self.train.client.patch(
                        f"/repos/{self.train.owner_login}/{self.train.repo_name}/git/refs/heads/{escaped_branch_name}",
                        json={"sha": self.initial_current_base_sha, "force": True},
                    )
                except http.HTTPClientSideError as exc_patch:
                    self._report_failure(exc_patch)
                    raise TrainCarPullRequestCreationFailure(self) from exc_patch
            else:
                self._report_failure(exc)
                raise TrainCarPullRequestCreationFailure(self) from exc

        for pull_number in self.parents + [self.pull_request_number]:
            # TODO(sileht): if a merge fail we should update the summary of self.pull_request_number with
            # the failure
            try:
                self.train.client.post(
                    f"/repos/{self.train.owner_login}/{self.train.repo_name}/merges",
                    json={
                        "base": branch_name,
                        "head": f"refs/pull/{pull_number}/head",
                        "commit_message": f"Merge of #{pull_number}",
                    },
                )
            except http.HTTPClientSideError as e:
                self._report_failure(e)
                raise TrainCarPullRequestCreationFailure(self) from e

        try:
            pulls_for_title = ", ".join(
                [f"#{p}" for p in self.parents + [self.pull_request_number]]
            )
            title = f"merge-queue: testing {self.initial_current_base_sha[:7]}, {pulls_for_title} together"
            body = ""
            tmp_pull = self.train.client.post(
                f"/repos/{self.train.owner_login}/{self.train.repo_name}/pulls",
                json={
                    "title": title,
                    "body": body,
                    "base": self.train.ref,
                    "head": branch_name,
                },
            ).json()
        except http.HTTPClientSideError as e:
            self._report_failure(e)
            raise TrainCarPullRequestCreationFailure(self) from e

        self.tmp_pull_request_number = tmp_pull["number"]

        sub = utils.async_run(
            subscription.Subscription.get_subscription(self.train.owner_id)
        )
        self.update_summaries(
            context.Context(self.train.client, tmp_pull, sub),
            None,
            check_api.Conclusion.PENDING,
            False,
        )

    def delete_pull(self) -> None:
        if not self.tmp_pull_request_number:
            return

        branch = f"{constants.MERGE_QUEUE_BRANCH_PREFIX}/{self.train.ref}/{self.pull_request_number}"
        try:
            self.train.client.delete(
                f"/repos/{self.train.owner_login}/{self.train.repo_name}/git/refs/heads/{branch}"
            )
        except http.HTTPNotFound:
            pass

    def _report_failure(self, exception: http.HTTPClientSideError) -> None:
        # Update the original Pull Request
        pull: github_types.GitHubPullRequest = self.train.client.item(
            f"/repos/{self.train.owner_login}/{self.train.repo_name}/pulls/{self.pull_request_number}"
        )

        sub = utils.async_run(
            subscription.Subscription.get_subscription(self.train.owner_id)
        )
        original_ctxt = context.Context(self.train.client, pull, sub, [])

        title = "This pull request cannot tested to be merged"
        summary = f"This pull request can't be tested to be merged because: {exception.message}"
        original_ctxt.log.info(
            "fail to create the pull request train car",
            status=check_api.Conclusion.ACTION_REQUIRED,
            title=title,
            summary=summary,
        )
        check_api.set_check_run(
            original_ctxt,
            constants.MERGE_QUEUE_SUMMARY_NAME,
            check_api.Result(
                check_api.Conclusion.ACTION_REQUIRED,
                title=title,
                summary=summary,
            ),
        )

        utils.async_run(github_events.send_refresh(original_ctxt.pull))

    def update_summaries(
        self,
        tmp_pull_ctxt: context.Context,
        queue_rule: typing.Optional[rules.EvaluatedQueueRule],
        status: check_api.Conclusion,
        deleted: bool,
    ) -> None:
        if status == check_api.Conclusion.SUCCESS:
            title = f"The pull request #{self.pull_request_number} can be merged"
        elif status == check_api.Conclusion.PENDING:
            title = (
                f"The pull request #{self.pull_request_number} is tested to be merged"
            )
        else:
            title = f"The pull request #{self.pull_request_number} cannot be merged"

        if queue_rule:
            queue_summary = "\n\nRequired conditions for merge:\n"
            for cond in queue_rule.conditions:
                checked = " " if cond in queue_rule.missing_conditions else "X"
                queue_summary += f"\n- [{checked}] `{cond}`"
        else:
            queue_summary = ""

        pulls_for_title = ", ".join(
            [f"#{p}" for p in self.parents + [self.pull_request_number]]
        )
        summary = (
            f"Testing {self.initial_current_base_sha[:7]}, {pulls_for_title} together"
        )
        summary += queue_summary

        utils.async_run(
            tmp_pull_ctxt.set_summary_check(
                check_api.Result(
                    status,
                    title=title,
                    summary=summary,
                )
            )
        )

        # Update the original Pull Request
        pull: github_types.GitHubPullRequest = self.train.client.item(
            f"/repos/{self.train.owner_login}/{self.train.repo_name}/pulls/{self.pull_request_number}"
        )

        sub = utils.async_run(
            subscription.Subscription.get_subscription(self.train.owner_id)
        )
        original_ctxt = context.Context(self.train.client, pull, sub, [])
        title = f"The pull request is tested against {original_ctxt.pull['base']['ref']} to be merged"
        if deleted:
            # TODO(sileht): display train cars ?
            summary = "The pull request is going to be tested soon"
        else:
            summary = f"You can follow the pull request testing here: {tmp_pull_ctxt.pull['html_url']}"
        summary += queue_summary

        original_ctxt.log.info(
            "pull request train car status update",
            status=status,
            title=title,
            summary=summary,
        )
        check_api.set_check_run(
            original_ctxt,
            constants.MERGE_QUEUE_SUMMARY_NAME,
            check_api.Result(
                status,
                title=title,
                summary=summary,
            ),
        )

        utils.async_run(github_events.send_refresh(original_ctxt.pull))


@dataclasses.dataclass
class Train:
    client: github.GithubInstallationClient
    redis: redis.Redis
    owner_id: int
    owner_login: str
    repo_id: int
    repo_name: str
    ref: str
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
        return f"merge-train~{self.owner_id}~{self.repo_id}~{self.ref}"

    def __post_init__(self):
        train_raw = self.redis.get(self._get_train_queue_key())
        if train_raw:
            train = self.Serialized(json.loads(train_raw))
            self._waiting_pulls = [WaitingPull(*wp) for wp in train["waiting_pulls"]]
            self._current_base_sha = train["current_base_sha"]
            self._cars = [TrainCar.deserialized(self, c) for c in train["cars"]]

    @property
    def log(self):
        return daiquiri.getLogger(
            __name__,
            gh_owner=self.owner_login,
            gh_repo=self.repo_name,
            gh_branch=self.ref,
            max_size=self.max_size,
            train_cars=[c.pull_request_number for c in self._cars],
            train_waiting_pulls=[wp.pull_request_number for wp in self._waiting_pulls],
        )

    def _save(self):
        prepared = self.Serialized(
            waiting_pulls=self._waiting_pulls,
            current_base_sha=self._current_base_sha,
            cars=[c.serialized() for c in self._cars],
        )
        raw = json.dumps(prepared)
        self.redis.set(self._get_train_queue_key(), raw)

    def get_car_by_tmp_pull(self, ctxt: context.Context) -> typing.Optional[TrainCar]:
        for car in self._cars:
            if car.tmp_pull_request_number == ctxt.pull["number"]:
                return car
        return None

    def refresh(self) -> None:
        self._populate_cars()
        self._save()
        self.log.info("refreshed train cars")

    def reset(self) -> None:
        self._slice_cars_at(0)
        self._populate_cars()
        self._save()
        self.log.info("resetted train cars")

    def _slice_cars_at(self, position: int) -> None:
        for c in reversed(self._cars[position:]):
            self._waiting_pulls.insert(
                0, WaitingPull(c.pull_request_number, c.queue_name)
            )
            c.delete_pull()
        self._cars = self._cars[:position]

    def insert_pull_at(
        self, ctxt: context.Context, position: int, queue_name: rules.QueueName
    ) -> None:
        ctxt.log.log(42, "ADDING to train", position=position, queue_name=queue_name)
        self._slice_cars_at(position)
        self._waiting_pulls.insert(
            position - len(self._cars), WaitingPull(ctxt.pull["number"], queue_name)
        )
        self._populate_cars()
        self._save()
        ctxt.log.info("added to train", position=position, queue_name=queue_name)

    def remove_pull(self, ctxt: context.Context) -> None:
        if (
            ctxt.pull["merged"]
            and self._cars
            and ctxt.pull["number"] == self._cars[0].pull_request_number
            and self.is_synced_with_the_base_branch()
        ):
            # Head of the train was merged and the base_sha haven't changed, we can keep
            # other running cars
            self._cars[0].delete_pull()
            self._cars = self._cars[1:]

            if ctxt.pull["merge_commit_sha"] is None:
                raise RuntimeError("merged pull request without merge_commit_sha set")

            self._current_base_sha = ctxt.pull["merge_commit_sha"]

            for car in self._cars:
                car.current_base_sha = self._current_base_sha

            self._populate_cars()
            self._save()
            ctxt.log.info("removed from train", position=0)
            return

        ctxt.log.log(42, "REMOVING from train")
        position = (
            [c.pull_request_number for c in self._cars]
            + [wp.pull_request_number for wp in self._waiting_pulls]
        ).index(ctxt.pull["number"])
        self._slice_cars_at(position)
        del self._waiting_pulls[0]
        self._populate_cars()
        self._save()
        ctxt.log.info("removed from train", position=position)

    def _populate_cars(self) -> None:
        if self._current_base_sha is None or not self._cars:
            self._current_base_sha = self.get_head_sha()

        missing_cars = self.max_size - len(self._cars)

        if missing_cars < 0:
            # Too many cars
            to_delete = self._cars[missing_cars:]
            for car in to_delete:
                car.delete_pull()
                self._waiting_pulls.append(
                    WaitingPull(car.pull_request_number, car.queue_name)
                )
            self._cars = self._cars[: self.max_size]

        elif missing_cars > 0 and self._waiting_pulls:
            if self._cars and self._cars[-1].tmp_pull_request_number is None:
                # NOTE(sileht): the last created pull request have failed, so don't create
                # the next one, we wait it got removed from the queue
                return

            # Not enough cars
            for _ in range(missing_cars):
                try:
                    pull, queue_name = self._waiting_pulls.pop(0)
                except IndexError:
                    break

                parents = [car.pull_request_number for car in self._cars]
                car = TrainCar(
                    self,
                    pull,
                    queue_name,
                    parents,
                    self._current_base_sha,
                    self._current_base_sha,
                )
                self._cars.append(car)
                try:
                    car.create_pull()
                except TrainCarPullRequestCreationFailure:
                    # NOTE(sileht): We posted failure merge-queue check-run on car.pull
                    # and refreshed it, so it will be removed from the train soon. We
                    # don't need to create remaining cars now. When this car will be
                    # removed the remaining one will be created
                    return

    def get_head_sha(self) -> github_types.SHAType:
        escaped_branch_name = parse.quote(self.ref, safe="")
        return typing.cast(
            github_types.GitHubBranch,
            self.client.get(
                f"repos/{self.owner_login}/{self.repo_name}/branches/{escaped_branch_name}"
            ).json(),
        )["commit"]["sha"]

    def is_synced_with_the_base_branch(self) -> bool:
        if not self._cars:
            return True

        head_sha = self.get_head_sha()
        if head_sha == self._current_base_sha:
            return True

        merged_pulls = [
            p
            for p in self.client.items(
                f"repos/{self.owner_login}/{self.repo_name}/commits/{head_sha}/pulls",
                api_version="groot",
            )
            if self.client.get(
                f"repos/{self.owner_login}/{self.repo_name}/pulls/{p['number']}/merge"
            ).status_code
            == 204
        ]

        if len(merged_pulls) == 0:
            return False
        elif len(merged_pulls) > 1:
            self.log.error(
                "sha got merged by more that one pull request ???",
                gh_pulls=[p["number"] for p in merged_pulls],
            )
            return False

        # Base branch just moved but the last merged PR is the one we have on top on our
        # train, we just not yet received the event that have called Train.remove_pull()
        # NOTE(sileht): I wonder if it's robust enough, these cases should be enough to
        # catch everything I have in mind
        # * We run it when we remove the top car
        # * We run it when a tmp PR is refreshed
        # * We run it on each push events
        head_pull = merged_pulls[0]
        if self._cars[0].pull_request_number == head_pull["number"]:
            return True

        return False
