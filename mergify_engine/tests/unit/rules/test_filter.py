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
import datetime

from freezegun import freeze_time
import pytest

from mergify_engine.rules import filter
from mergify_engine.rules import parser


pytestmark = pytest.mark.asyncio


class FakePR(dict):  # type: ignore[type-arg]
    def __getattr__(self, k):
        return self[k]


async def test_binary() -> None:
    f = filter.BinaryFilter({"=": ("foo", 1)})
    assert await f(FakePR({"foo": 1}))
    assert not await f(FakePR({"foo": 2}))


async def test_string() -> None:
    f = filter.BinaryFilter({"=": ("foo", "bar")})
    assert await f(FakePR({"foo": "bar"}))
    assert not await f(FakePR({"foo": 2}))


async def test_not() -> None:
    f = filter.BinaryFilter({"-": {"=": ("foo", 1)}})
    assert not await f(FakePR({"foo": 1}))
    assert await f(FakePR({"foo": 2}))


async def test_len() -> None:
    f = filter.BinaryFilter({"=": ("#foo", 3)})
    assert await f(FakePR({"foo": "bar"}))
    with pytest.raises(filter.InvalidOperator):
        await f(FakePR({"foo": 2}))
    assert not await f(FakePR({"foo": "a"}))
    assert not await f(FakePR({"foo": "abcedf"}))
    assert await f(FakePR({"foo": [10, 20, 30]}))
    assert not await f(FakePR({"foo": [10, 20]}))
    assert not await f(FakePR({"foo": [10, 20, 40, 50]}))
    f = filter.BinaryFilter({">": ("#foo", 3)})
    assert await f(FakePR({"foo": "barz"}))
    with pytest.raises(filter.InvalidOperator):
        await f(FakePR({"foo": 2}))
    assert not await f(FakePR({"foo": "a"}))
    assert await f(FakePR({"foo": "abcedf"}))
    assert await f(FakePR({"foo": [10, "abc", 20, 30]}))
    assert not await f(FakePR({"foo": [10, 20]}))
    assert not await f(FakePR({"foo": []}))


async def test_regexp() -> None:
    f = filter.BinaryFilter({"~=": ("foo", "^f")})
    assert await f(FakePR({"foo": "foobar"}))
    assert await f(FakePR({"foo": "foobaz"}))
    assert not await f(FakePR({"foo": "x"}))
    assert not await f(FakePR({"foo": None}))

    f = filter.BinaryFilter({"~=": ("foo", "^$")})
    assert await f(FakePR({"foo": ""}))
    assert not await f(FakePR({"foo": "x"}))


async def test_regexp_invalid() -> None:
    with pytest.raises(filter.InvalidArguments):
        filter.BinaryFilter({"~=": ("foo", r"([^\s\w])(\s*\1+")})


async def test_set_value_expanders() -> None:
    f = filter.BinaryFilter(
        {"=": ("foo", "@bar")},
    )
    f.value_expanders["foo"] = lambda x: [x.replace("@", "foo")]
    assert await f(FakePR({"foo": "foobar"}))
    assert not await f(FakePR({"foo": "x"}))


async def test_set_value_expanders_unset_at_init() -> None:
    f = filter.BinaryFilter({"=": ("foo", "@bar")})
    f.value_expanders = {"foo": lambda x: [x.replace("@", "foo")]}
    assert await f(FakePR({"foo": "foobar"}))
    assert not await f(FakePR({"foo": "x"}))


async def test_does_not_contain() -> None:
    f = filter.BinaryFilter({"!=": ("foo", 1)})
    assert await f(FakePR({"foo": []}))
    assert await f(FakePR({"foo": [2, 3]}))
    assert not await f(FakePR({"foo": (1, 2)}))


async def test_set_value_expanders_does_not_contain() -> None:
    f = filter.BinaryFilter({"!=": ("foo", "@bar")})
    f.value_expanders["foo"] = lambda x: ["foobaz", "foobar"]
    assert not await f(FakePR({"foo": "foobar"}))
    assert not await f(FakePR({"foo": "foobaz"}))
    assert await f(FakePR({"foo": "foobiz"}))


async def test_contains() -> None:
    f = filter.BinaryFilter({"=": ("foo", 1)})
    assert await f(FakePR({"foo": [1, 2]}))
    assert not await f(FakePR({"foo": [2, 3]}))
    assert await f(FakePR({"foo": (1, 2)}))
    f = filter.BinaryFilter({">": ("foo", 2)})
    assert not await f(FakePR({"foo": [1, 2]}))
    assert await f(FakePR({"foo": [2, 3]}))


async def test_unknown_attribute() -> None:
    f = filter.BinaryFilter({"=": ("foo", 1)})
    with pytest.raises(filter.UnknownAttribute):
        await f(FakePR({"bar": 1}))


async def test_parse_error() -> None:
    with pytest.raises(filter.ParseError):
        filter.BinaryFilter({})


async def test_unknown_operator() -> None:
    with pytest.raises(filter.UnknownOperator):
        filter.BinaryFilter({"oops": (1, 2)})  # type: ignore[arg-type]


async def test_invalid_arguments() -> None:
    with pytest.raises(filter.InvalidArguments):
        filter.BinaryFilter({"=": (1, 2, 3)})  # type: ignore[typeddict-item]


async def test_str() -> None:
    assert "foo~=^f" == str(filter.BinaryFilter({"~=": ("foo", "^f")}))
    assert "-foo=1" == str(filter.BinaryFilter({"-": {"=": ("foo", 1)}}))
    assert "foo" == str(filter.BinaryFilter({"=": ("foo", True)}))
    assert "-bar" == str(filter.BinaryFilter({"=": ("bar", False)}))
    with pytest.raises(filter.InvalidOperator):
        str(filter.BinaryFilter({">=": ("bar", False)}))


def time(hour: int, minute: int) -> datetime.time:
    return datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc)


def dtime(hour: int, minute: int) -> datetime.datetime:
    return datetime.datetime.utcnow().replace(
        hour=hour, minute=minute, tzinfo=datetime.timezone.utc
    )


@freeze_time("2012-01-14")
async def test_datetime_binary() -> None:
    assert "foo>=2012-01-14T00:00:00" == str(
        filter.BinaryFilter({">=": ("foo", dtime(0, 0))})
    )
    assert "foo<=2012-01-14T23:59:00" == str(
        filter.BinaryFilter({"<=": ("foo", dtime(23, 59))})
    )
    assert "foo<=2012-01-14T03:09:00" == str(
        filter.BinaryFilter({"<=": ("foo", dtime(3, 9))})
    )

    f = filter.BinaryFilter({"<=": ("foo", dtime(5, 8))})
    assert await f(FakePR({"foo": dtime(5, 8)}))
    assert await f(FakePR({"foo": dtime(2, 1)}))
    assert await f(FakePR({"foo": dtime(5, 1)}))
    assert not await f(FakePR({"foo": dtime(6, 2)}))
    assert not await f(FakePR({"foo": dtime(8, 9)}))

    f = filter.BinaryFilter({">=": ("foo", dtime(5, 8))})
    assert await f(FakePR({"foo": dtime(5, 8)}))
    assert not await f(FakePR({"foo": dtime(2, 1)}))
    assert not await f(FakePR({"foo": dtime(5, 1)}))
    assert await f(FakePR({"foo": dtime(6, 2)}))
    assert await f(FakePR({"foo": dtime(8, 9)}))


@freeze_time("2012-01-14")
async def test_time_binary() -> None:
    assert "foo>=00:00" == str(filter.BinaryFilter({">=": ("foo", time(0, 0))}))
    assert "foo<=23:59" == str(filter.BinaryFilter({"<=": ("foo", time(23, 59))}))
    assert "foo<=03:09" == str(filter.BinaryFilter({"<=": ("foo", time(3, 9))}))

    f = filter.BinaryFilter({"<=": ("foo", time(5, 8))})
    assert await f(FakePR({"foo": time(5, 8)}))
    assert await f(FakePR({"foo": time(2, 1)}))
    assert await f(FakePR({"foo": time(5, 1)}))
    assert not await f(FakePR({"foo": time(6, 2)}))
    assert not await f(FakePR({"foo": time(8, 9)}))

    f = filter.BinaryFilter({">=": ("foo", time(5, 8))})
    assert await f(FakePR({"foo": time(5, 8)}))
    assert not await f(FakePR({"foo": time(2, 1)}))
    assert not await f(FakePR({"foo": time(5, 1)}))
    assert await f(FakePR({"foo": time(6, 2)}))
    assert await f(FakePR({"foo": time(8, 9)}))


@freeze_time("2012-01-14T12:15:00")
async def test_time_near_datetime() -> None:
    atnoon = datetime.datetime(2012, 1, 15, 0, 0, 0, 0, tzinfo=datetime.timezone.utc)
    nextday = datetime.datetime(2012, 1, 15, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    today = datetime.datetime(2012, 1, 14, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    soon = datetime.datetime(2012, 1, 14, 5, 13, 0, 0, tzinfo=datetime.timezone.utc)

    f = filter.NearDatetimeFilter({"<=": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == atnoon
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon

    f = filter.NearDatetimeFilter({"<": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == atnoon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == atnoon
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon

    f = filter.NearDatetimeFilter({">=": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == atnoon
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon

    f = filter.NearDatetimeFilter({">": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == atnoon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == atnoon
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon

    f = filter.NearDatetimeFilter({"=": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == nextday
    assert await f(FakePR({"foo": time(8, 9)})) == nextday

    f = filter.NearDatetimeFilter({"!=": ("foo", today.timetz())})
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(5, 1)})) == today
    assert await f(FakePR({"foo": time(6, 2)})) == nextday
    assert await f(FakePR({"foo": time(8, 9)})) == nextday


@freeze_time("2012-01-14T12:15:00")
async def test_datetime_near_datetime() -> None:
    today = datetime.datetime(2012, 1, 14, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    soon = datetime.datetime(2012, 1, 14, 5, 13, 0, 0, tzinfo=datetime.timezone.utc)

    f = filter.NearDatetimeFilter({"<=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY

    f = filter.NearDatetimeFilter({"<": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY

    f = filter.NearDatetimeFilter({">=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY

    f = filter.NearDatetimeFilter({">": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY

    f = filter.NearDatetimeFilter({"=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY

    f = filter.NearDatetimeFilter({"!=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_INFINITY
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_INFINITY


@freeze_time("2012-01-14T12:05:00")
async def test_multiple_near_datetime() -> None:
    atnoon = datetime.datetime(2012, 1, 15, 0, 0, 0, 0, tzinfo=datetime.timezone.utc)
    today = datetime.datetime(2012, 1, 14, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    in_two_hours = datetime.datetime(
        2012, 1, 14, 7, 5, 0, 0, tzinfo=datetime.timezone.utc
    )
    soon = datetime.datetime(2012, 1, 14, 5, 13, 0, 0, tzinfo=datetime.timezone.utc)

    f = filter.NearDatetimeFilter(
        {
            "or": [
                {"<=": ("foo", in_two_hours.timetz())},
                {"<=": ("foo", today.timetz())},
            ]
        }
    )
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(6, 8)})) == in_two_hours
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon
    assert await f(FakePR({"foo": time(18, 9)})) == atnoon

    f = filter.NearDatetimeFilter(
        {
            "and": [
                {">=": ("foo", in_two_hours.timetz())},
                {">=": ("foo", today.timetz())},
            ]
        }
    )
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(6, 8)})) == in_two_hours
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon
    assert await f(FakePR({"foo": time(18, 9)})) == atnoon

    f = filter.NearDatetimeFilter(
        {
            "or": [
                {">=": ("foo", in_two_hours.timetz())},
                {">=": ("foo", today.timetz())},
            ]
        }
    )
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(6, 8)})) == in_two_hours
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon
    assert await f(FakePR({"foo": time(18, 9)})) == atnoon

    f = filter.NearDatetimeFilter(
        {
            "and": [
                {"<=": ("foo", in_two_hours.timetz())},
                {"<=": ("foo", today.timetz())},
            ]
        }
    )
    assert await f(FakePR({"foo": time(5, 8)})) == soon
    assert await f(FakePR({"foo": time(2, 1)})) == today
    assert await f(FakePR({"foo": time(6, 8)})) == in_two_hours
    assert await f(FakePR({"foo": time(8, 9)})) == atnoon
    assert await f(FakePR({"foo": time(18, 9)})) == atnoon


async def test_or() -> None:
    f = filter.BinaryFilter({"or": ({"=": ("foo", 1)}, {"=": ("bar", 1)})})
    assert await f(FakePR({"foo": 1, "bar": 1}))
    assert not await f(FakePR({"bar": 2, "foo": 2}))
    assert await f(FakePR({"bar": 2, "foo": 1}))
    assert await f(FakePR({"bar": 1, "foo": 2}))


async def test_and() -> None:
    f = filter.BinaryFilter({"and": ({"=": ("foo", 1)}, {"=": ("bar", 1)})})
    assert await f(FakePR({"bar": 1, "foo": 1}))
    assert not await f(FakePR({"bar": 2, "foo": 2}))
    assert not await f(FakePR({"bar": 2, "foo": 1}))
    assert not await f(FakePR({"bar": 1, "foo": 2}))
    with pytest.raises(filter.ParseError):
        filter.BinaryFilter({"or": {"foo": "whar"}})


async def test_chain() -> None:
    f1 = {"=": ("bar", 1)}
    f2 = {"=": ("foo", 1)}
    f = filter.BinaryFilter({"and": (f1, f2)})
    assert await f(FakePR({"bar": 1, "foo": 1}))
    assert not await f(FakePR({"bar": 2, "foo": 2}))
    assert not await f(FakePR({"bar": 2, "foo": 1}))
    assert not await f(FakePR({"bar": 1, "foo": 2}))


async def test_parser_leaf() -> None:
    for string in ("head=foobar", "-base=master", "#files>3"):
        tree = parser.search.parseString(string, parseAll=True)[0]
        assert string == str(filter.BinaryFilter(tree))


async def test_parser_group() -> None:
    string = str(
        filter.BinaryFilter({"and": ({"=": ("head", "foobar")}, {">": ("#files", 3)})})
    )
    assert string == "(head=foobar and #files>3)"
