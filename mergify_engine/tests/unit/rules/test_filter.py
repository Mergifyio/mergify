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
    f = filter.Filter({
        "=": ("foo", 1),
    })
    assert f(foo=1)
    assert not f(foo=2)


def test_string():
    f = filter.Filter({
        "=": ("foo", "bar"),
    })
    assert f(foo="bar")
    assert not f(foo=2)


def test_not():
    f = filter.Filter({
        "-": {"=": ("foo", 1)},
    })
    assert not f(foo=1)
    assert f(foo=2)


def test_len():
    f = filter.Filter({
        "=": ("#foo", 3),
    })
    assert f(foo="bar")
    with pytest.raises(filter.InvalidOperator):
        f(foo=2)
    assert not f(foo="a")
    assert not f(foo="abcedf")
    assert f(foo=[10, 20, 30])
    assert not f(foo=[10, 20])
    assert not f(foo=[10, 20, 40, 50])
    f = filter.Filter({
        ">": ("#foo", 3),
    })
    assert f(foo="barz")
    with pytest.raises(filter.InvalidOperator):
        f(foo=2)
    assert not f(foo="a")
    assert f(foo="abcedf")
    assert f(foo=[10, "abc", 20, 30])
    assert not f(foo=[10, 20])
    assert not f(foo=[])


def test_regexp():
    f = filter.Filter({
        "~=": ("foo", "^f"),
    })
    assert f(foo="foobar")
    assert f(foo="foobaz")
    assert not f(foo="x")


def test_contains():
    f = filter.Filter({
        "=": ("foo", 1),
    })
    assert f(foo=[1, 2])
    assert not f(foo=[2, 3])
    assert f(foo=(1, 2))
    f = filter.Filter({
        ">": ("foo", 2),
    })
    assert not f(foo=[1, 2])
    assert f(foo=[2, 3])


def test_unknown_attribute():
    f = filter.Filter({
        "=": ("foo", 1),
    })
    with pytest.raises(filter.UnknownAttribute):
        f(bar=1)


def test_parse_error():
    with pytest.raises(filter.ParseError):
        filter.Filter({})
    with pytest.raises(filter.ParseError):
        filter.Filter({"and": [], "or": []})


def test_unknown_operator():
    with pytest.raises(filter.UnknownOperator):
        filter.Filter({
            "oops": (1, 2),
        })


def test_invalid_arguments():
    with pytest.raises(filter.InvalidArguments):
        filter.Filter({
            "=": (1, 2, 3),
        })


def test_str():
    assert "foo~=^f" == str(filter.Filter({
        "~=": ("foo", "^f"),
    }))
    assert "-foo=1" == str(filter.Filter({
        "-": {"=": ("foo", 1)},
    }))
    assert "(foo=1 and bar>2)" == str(filter.Filter({
        "and": [
            {"=": ("foo", 1)},
            {">": ("bar", 2)},
        ],
    }))
    assert "(foo=1 and bar>2 and (baz~=3 or xz<lol))" == str(filter.Filter({
        "and": [
            {"=": ("foo", 1)},
            {">": ("bar", 2)},
            {"or": [
                {"~=": ("baz", 3)},
                {"<": ("xz", "lol")},
            ]},
        ],
    }))


def test_parser():
    for string in (
            "head=foobar",
            "-base=master",
            "#files>3",
    ):
        assert string == str(filter.Filter.parse(string))
