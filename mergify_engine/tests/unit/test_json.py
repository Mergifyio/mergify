# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
import datetime
import enum
import json

import pytest

from mergify_engine import json as mergify_json


class Color(enum.Enum):
    RED = 1
    GREEN = 2
    BLUE = 3


payload_decoded = {
    "name": "hello",
    "conditions": [],
    "actions": {"merge": {"strict": Color.BLUE}},
    "datetime_naive": datetime.datetime(2021, 5, 15, 8, 35, 36, 442306),
    "datetime_aware": datetime.datetime(
        2021, 5, 15, 8, 41, 36, 796485, tzinfo=datetime.timezone.utc
    ),
}

payload_encoded = {
    "name": "hello",
    "conditions": [],
    "actions": {
        "merge": {"strict": {"__pytype__": "enum", "class": "Color", "name": "BLUE"}}
    },
    "datetime_naive": {
        "__pytype__": "datetime.datetime",
        "value": "2021-05-15T08:35:36.442306",
    },
    "datetime_aware": {
        "__pytype__": "datetime.datetime",
        "value": "2021-05-15T08:41:36.796485+00:00",
    },
}

mergify_json.register_type(Color)


def test_register_type_fail() -> None:
    with pytest.raises(RuntimeError):
        mergify_json.register_type(Color)


def test_encode() -> None:
    assert json.loads(mergify_json.dumps(payload_decoded)) == payload_encoded


def test_decode() -> None:
    json_file = json.dumps(payload_encoded)
    assert mergify_json.loads(json_file) == payload_decoded
