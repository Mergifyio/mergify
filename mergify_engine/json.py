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
import enum
import json


JSON_TYPES = {}


def register_type(enum_cls):
    JSON_TYPES[enum_cls.__name__] = enum_cls


class Encoder(json.JSONEncoder):
    def default(self, v):
        if isinstance(v, enum.Enum):
            return {
                "__pytype__": "enum",
                "class": type(v).__name__,
                "name": v.name,
            }
        else:
            return super().default(v)


def decode_enum(v):
    if v.get("__pytype__") == "enum":
        cls_name = v["class"]
        enum_cls = JSON_TYPES[cls_name]
        enum_name = v["name"]
        return enum_cls[enum_name]
    return v


def dumps(v):
    return json.dumps(v, cls=Encoder)


def loads(v):
    return json.loads(v, object_hook=decode_enum)
