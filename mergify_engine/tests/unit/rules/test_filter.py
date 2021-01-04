# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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
import pytest

from mergify_engine.rules import filter


class FakePR(dict):  # type: ignore[type-arg]
    def __getattr__(self, k):
        return self[k]


def test_binary() -> None:
    f = filter.Filter({"=": ("foo", 1)})
    assert f(FakePR({"foo": 1}))
    assert not f(FakePR({"foo": 2}))


def test_string() -> None:
    f = filter.Filter({"=": ("foo", "bar")})
    assert f(FakePR({"foo": "bar"}))
    assert not f(FakePR({"foo": 2}))


def test_not() -> None:
    f = filter.Filter({"-": {"=": ("foo", 1)}})
    assert not f(FakePR({"foo": 1}))
    assert f(FakePR({"foo": 2}))


def test_len() -> None:
    f = filter.Filter({"=": ("#foo", 3)})
    assert f(FakePR({"foo": "bar"}))
    with pytest.raises(filter.InvalidOperator):
        f(FakePR({"foo": 2}))
    assert not f(FakePR({"foo": "a"}))
    assert not f(FakePR({"foo": "abcedf"}))
    assert f(FakePR({"foo": [10, 20, 30]}))
    assert not f(FakePR({"foo": [10, 20]}))
    assert not f(FakePR({"foo": [10, 20, 40, 50]}))
    f = filter.Filter({">": ("#foo", 3)})
    assert f(FakePR({"foo": "barz"}))
    with pytest.raises(filter.InvalidOperator):
        f(FakePR({"foo": 2}))
    assert not f(FakePR({"foo": "a"}))
    assert f(FakePR({"foo": "abcedf"}))
    assert f(FakePR({"foo": [10, "abc", 20, 30]}))
    assert not f(FakePR({"foo": [10, 20]}))
    assert not f(FakePR({"foo": []}))


def test_regexp() -> None:
    f = filter.Filter({"~=": ("foo", "^f")})
    assert f(FakePR({"foo": "foobar"}))
    assert f(FakePR({"foo": "foobaz"}))
    assert not f(FakePR({"foo": "x"}))
    assert not f(FakePR({"foo": None}))

    f = filter.Filter({"~=": ("foo", "^$")})
    assert f(FakePR({"foo": ""}))
    assert not f(FakePR({"foo": "x"}))


def test_regexp_invalid() -> None:
    with pytest.raises(filter.InvalidArguments):
        filter.Filter({"~=": ("foo", r"([^\s\w])(\s*\1+")})


def test_set_value_expanders() -> None:
    f = filter.Filter(
        {"=": ("foo", "@bar")},
        value_expanders={"foo": lambda x: [x.replace("@", "foo")]},
    )
    assert f(FakePR({"foo": "foobar"}))
    assert not f(FakePR({"foo": "x"}))


def test_set_value_expanders_unset_at_init() -> None:
    f = filter.Filter({"=": ("foo", "@bar")})
    f.value_expanders = {"foo": lambda x: [x.replace("@", "foo")]}
    assert f(FakePR({"foo": "foobar"}))
    assert not f(FakePR({"foo": "x"}))


def test_does_not_contain() -> None:
    f = filter.Filter({"!=": ("foo", 1)})
    assert f(FakePR({"foo": []}))
    assert f(FakePR({"foo": [2, 3]}))
    assert not f(FakePR({"foo": (1, 2)}))


def test_set_value_expanders_does_not_contain() -> None:
    f = filter.Filter(
        {"!=": ("foo", "@bar")}, value_expanders={"foo": lambda x: ["foobaz", "foobar"]}
    )
    assert not f(FakePR({"foo": "foobar"}))
    assert not f(FakePR({"foo": "foobaz"}))
    assert f(FakePR({"foo": "foobiz"}))


def test_contains() -> None:
    f = filter.Filter({"=": ("foo", 1)})
    assert f(FakePR({"foo": [1, 2]}))
    assert not f(FakePR({"foo": [2, 3]}))
    assert f(FakePR({"foo": (1, 2)}))
    f = filter.Filter({">": ("foo", 2)})
    assert not f(FakePR({"foo": [1, 2]}))
    assert f(FakePR({"foo": [2, 3]}))


def test_unknown_attribute() -> None:
    f = filter.Filter({"=": ("foo", 1)})
    with pytest.raises(filter.UnknownAttribute):
        f(FakePR({"bar": 1}))


def test_parse_error() -> None:
    with pytest.raises(filter.ParseError):
        filter.Filter({})


def test_unknown_operator() -> None:
    with pytest.raises(filter.UnknownOperator):
        filter.Filter({"oops": (1, 2)})  # type: ignore[typeddict-item]


def test_invalid_arguments() -> None:
    with pytest.raises(filter.InvalidArguments):
        filter.Filter({"=": (1, 2, 3)})  # type: ignore[typeddict-item]


def test_str() -> None:
    assert "foo~=^f" == str(filter.Filter({"~=": ("foo", "^f")}))
    assert "-foo=1" == str(filter.Filter({"-": {"=": ("foo", 1)}}))
    assert "foo" == str(filter.Filter({"=": ("foo", True)}))
    assert "-bar" == str(filter.Filter({"=": ("bar", False)}))
    with pytest.raises(filter.InvalidOperator):
        str(filter.Filter({">=": ("bar", False)}))


def test_parser() -> None:
    for string in ("head=foobar", "-base=master", "#files>3"):
        assert string == str(filter.Filter.parse(string))
