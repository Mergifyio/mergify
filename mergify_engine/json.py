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
import datetime
import enum
import json
import typing


_JSON_TYPES = {}


def register_type(enum_cls: typing.Type[enum.Enum]) -> None:
    if enum_cls.__name__ in _JSON_TYPES:
        raise RuntimeError(f"{enum_cls.__name__} already registered")
    else:
        _JSON_TYPES[enum_cls.__name__] = enum_cls


class Encoder(json.JSONEncoder):
    def default(self, v: typing.Any) -> typing.Any:
        if isinstance(v, enum.Enum):
            return {
                "__pytype__": "enum",
                "class": type(v).__name__,
                "name": v.name,
            }
        elif isinstance(v, datetime.datetime):
            return {
                "__pytype__": "datetime.datetime",
                "value": v.isoformat(),
            }
        else:
            return super().default(v)


JSONPyType = typing.Literal["enum"]


class JSONObjectDict(typing.TypedDict, total=False):
    __pytype__: JSONPyType


def _decode(v: typing.Dict[typing.Any, typing.Any]) -> typing.Any:
    if v.get("__pytype__") == "enum":
        cls_name = v["class"]
        enum_cls = _JSON_TYPES[cls_name]
        enum_name = v["name"]
        return enum_cls[enum_name]
    elif v.get("__pytype__") == "datetime.datetime":
        return datetime.datetime.fromisoformat(v["value"])
    return v


def dumps(v: typing.Any) -> str:
    return json.dumps(v, cls=Encoder)


def loads(v: typing.Union[str, bytes]) -> typing.Any:
    return json.loads(v, object_hook=_decode)
