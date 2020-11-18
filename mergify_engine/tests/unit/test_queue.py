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

import json

from mergify_engine import queue
from mergify_engine import utils


def test_queue_name_migration():
    r = utils.get_redis_for_cache()
    r.flushall()
    r.set(
        "strict-merge-config~12345~owner~repo~1",
        json.dumps({"fake": "config1"}),
    )
    r.set(
        "strict-merge-config~12345~owner~repo~2",
        json.dumps({"fake": "config2"}),
    )
    r.set(
        "strict-merge-config~12345~owner~repo~3",
        json.dumps({"fake": "config3"}),
    )
    r.set(
        "strict-merge-config~54321~foo~bar~42",
        json.dumps({"fake": "config42"}),
    )

    r.zadd("strict-merge-queues~12345~owner~repo~master", {1: 1000})
    r.zadd("strict-merge-queues~12345~owner~repo~stable", {2: 2000})
    r.zadd("strict-merge-queues~12345~owner~repo~master", {3: 5000})
    r.zadd("strict-merge-queues~54321~foo~bar~yo", {42: 5000})

    queue.Queue(r, owner_id=12, owner="owner", repo_id=34, repo="repo", ref="master")
    queue.Queue(r, owner_id=4, owner="foo", repo_id=2, repo="bar", ref="whatever")

    assert r.keys("strict-*") == []
    assert sorted(r.keys("merge-queue~*")) == [
        "merge-queue~12~34~master",
        "merge-queue~12~34~stable",
        "merge-queue~4~2~yo",
    ]
    assert sorted(r.keys("merge-config~*")) == [
        "merge-config~12~34~1",
        "merge-config~12~34~2",
        "merge-config~12~34~3",
        "merge-config~4~2~42",
    ]
    r.flushall()
