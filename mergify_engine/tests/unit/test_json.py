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
import enum
import json

from mergify_engine import json as mergify_json


class Color(enum.Enum):
    RED = 1
    GREEN = 2
    BLUE = 3


with_enum = {
    "name": "hello",
    "conditions": [],
    "actions": {"merge": {"strict": Color.BLUE}},
}

with_enum_encode = {
    "name": "hello",
    "conditions": [],
    "actions": {
        "merge": {"strict": {"__pytype__": "enum", "class": "Color", "name": "BLUE"}}
    },
}

mergify_json.register_type(type(Color.BLUE))


def test_encode_enum():
    assert json.loads(mergify_json.dumps(with_enum)) == with_enum_encode


def test_decode_enum():
    json_file = mergify_json.dumps(with_enum)
    assert mergify_json.loads(json_file) == with_enum
