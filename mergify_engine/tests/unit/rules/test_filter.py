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


def test_binary():
    f = filter.Filter({"=": ("foo", 1)})
    assert f(foo=1)
    assert not f(foo=2)


def test_string():
    f = filter.Filter({"=": ("foo", "bar")})
    assert f(foo="bar")
    assert not f(foo=2)


def test_not():
    f = filter.Filter({"-": {"=": ("foo", 1)}})
    assert not f(foo=1)
    assert f(foo=2)


def test_len():
    f = filter.Filter({"=": ("#foo", 3)})
    assert f(foo="bar")
    with pytest.raises(filter.InvalidOperator):
        f(foo=2)
    assert not f(foo="a")
    assert not f(foo="abcedf")
    assert f(foo=[10, 20, 30])
    assert not f(foo=[10, 20])
    assert not f(foo=[10, 20, 40, 50])
    f = filter.Filter({">": ("#foo", 3)})
    assert f(foo="barz")
    with pytest.raises(filter.InvalidOperator):
        f(foo=2)
    assert not f(foo="a")
    assert f(foo="abcedf")
    assert f(foo=[10, "abc", 20, 30])
    assert not f(foo=[10, 20])
    assert not f(foo=[])


def test_regexp():
    f = filter.Filter({"~=": ("foo", "^f")})
    assert f(foo="foobar")
    assert f(foo="foobaz")
    assert not f(foo="x")
    assert not f(foo=None)

    f = filter.Filter({"~=": ("foo", "^$")})
    assert f(foo="")
    assert not f(foo="x")


def test_regexp_invalid():
    with pytest.raises(filter.InvalidArguments):
        filter.Filter({"~=": ("foo", r"([^\s\w])(\s*\1+")})


def test_set_value_expanders():
    f = filter.Filter({"=": ("foo", "@bar")})
    f.set_value_expanders("foo", lambda x: [x.replace("@", "foo")])
    assert f(foo="foobar")
    assert not f(foo="x")


def test_does_not_contain():
    f = filter.Filter({"!=": ("foo", 1)})
    assert f(foo=[])
    assert f(foo=[2, 3])
    assert not f(foo=(1, 2))


def test_set_value_expanders_does_not_contain():
    f = filter.Filter({"!=": ("foo", "@bar")})
    f.set_value_expanders("foo", lambda x: ["foobaz", "foobar"])
    assert not f(foo="foobar")
    assert not f(foo="foobaz")
    assert f(foo="foobiz")


def test_contains():
    f = filter.Filter({"=": ("foo", 1)})
    assert f(foo=[1, 2])
    assert not f(foo=[2, 3])
    assert f(foo=(1, 2))
    f = filter.Filter({">": ("foo", 2)})
    assert not f(foo=[1, 2])
    assert f(foo=[2, 3])


def test_unknown_attribute():
    f = filter.Filter({"=": ("foo", 1)})
    with pytest.raises(filter.UnknownAttribute):
        f(bar=1)


def test_parse_error():
    with pytest.raises(filter.ParseError):
        filter.Filter({})


def test_unknown_operator():
    with pytest.raises(filter.UnknownOperator):
        filter.Filter({"oops": (1, 2)})


def test_invalid_arguments():
    with pytest.raises(filter.InvalidArguments):
        filter.Filter({"=": (1, 2, 3)})


def test_str():
    assert "foo~=^f" == str(filter.Filter({"~=": ("foo", "^f")}))
    assert "-foo=1" == str(filter.Filter({"-": {"=": ("foo", 1)}}))
    assert "foo" == str(filter.Filter({"=": ("foo", True)}))
    assert "-bar" == str(filter.Filter({"=": ("bar", False)}))
    with pytest.raises(filter.InvalidOperator):
        str(filter.Filter({">=": ("bar", False)}))


def test_parser():
    for string in ("head=foobar", "-base=master", "#files>3"):
        assert string == str(filter.Filter.parse(string))
