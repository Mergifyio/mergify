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
import typing

from freezegun import freeze_time
import pytest

from mergify_engine import date
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


@freeze_time("2012-01-14")
async def test_dow_str() -> None:
    assert "foo>=Fri" == str(filter.BinaryFilter({">=": ("foo", date.DayOfWeek(5))}))
    assert "foo<=Sun" == str(filter.BinaryFilter({"<=": ("foo", date.DayOfWeek(7))}))
    assert "foo=Wed" == str(filter.BinaryFilter({"=": ("foo", date.DayOfWeek(3))}))


@pytest.mark.parametrize(
    "klass",
    (
        date.Day,
        date.Month,
        date.Year,
    ),
)
@freeze_time("2012-01-14")
async def test_partial_datetime_str(
    klass: typing.Type[date.PartialDatetime],
) -> None:
    assert "foo>=5" == str(filter.BinaryFilter({">=": ("foo", klass(5))}))
    assert "foo<=11" == str(filter.BinaryFilter({"<=": ("foo", klass(11))}))
    assert "foo=3" == str(filter.BinaryFilter({"=": ("foo", klass(3))}))


@pytest.mark.parametrize(
    "klass",
    (
        date.Day,
        date.Month,
        date.Year,
        date.DayOfWeek,
    ),
)
@freeze_time("2012-01-14")
async def test_partial_datetime_binary(
    klass: typing.Type[date.PartialDatetime],
) -> None:

    f = filter.BinaryFilter({"<=": ("foo", klass(5))})
    assert await f(FakePR({"foo": klass(2)}))
    assert await f(FakePR({"foo": klass(5)}))
    assert not await f(FakePR({"foo": klass(7)}))

    f = filter.BinaryFilter({"=": ("foo", klass(5))})
    assert await f(FakePR({"foo": klass(5)}))
    assert not await f(FakePR({"foo": klass(7)}))

    f = filter.BinaryFilter({">": ("foo", klass(5))})
    assert await f(FakePR({"foo": klass(7)}))
    assert not await f(FakePR({"foo": klass(2)}))
    assert not await f(FakePR({"foo": klass(5)}))


async def test_day_near_datetime() -> None:
    with freeze_time("2012-01-06T12:15:00", tz_offset=0) as frozen_time:
        today = frozen_time().replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc
        )
        nextday = today.replace(day=today.day + 1)
        nextmonth = today.replace(month=today.month + 1, day=1)
        nextmonth_at_six = today.replace(month=today.month + 1, day=6)

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextday
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today

        f = filter.NearDatetimeFilter({"<": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextmonth
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextday
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today

        f = filter.NearDatetimeFilter({"<": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextmonth
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today

        f = filter.NearDatetimeFilter({"=": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextday
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth_at_six
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today

        f = filter.NearDatetimeFilter({"!=": ("foo", date.Day(6))})
        frozen_time.move_to(today.replace(day=6))
        assert await f(FakePR({"foo": date.Day(6)})) == nextday
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.Day(7)})) == nextmonth_at_six
        frozen_time.move_to(today.replace(day=1))
        assert await f(FakePR({"foo": date.Day(1)})) == today


async def test_day_of_the_week_near_datetime() -> None:
    # 2012-01-06 is a Friday (5)

    with freeze_time("2012-01-06T12:15:00", tz_offset=0) as frozen_time:
        today = frozen_time().replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc
        )
        on_saturday = today.replace(day=7)
        on_sunday = today.replace(day=8)
        next_saturday = today.replace(day=14)

        f = filter.NearDatetimeFilter({"<=": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == on_sunday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday

        f = filter.NearDatetimeFilter({"<": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == next_saturday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday

        f = filter.NearDatetimeFilter({"<=": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == on_sunday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday

        f = filter.NearDatetimeFilter({"<": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == next_saturday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday

        f = filter.NearDatetimeFilter({"=": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == on_sunday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday

        f = filter.NearDatetimeFilter({"!=": ("foo", date.DayOfWeek(6))})
        frozen_time.move_to(today.replace(day=7))
        assert await f(FakePR({"foo": date.DayOfWeek(6)})) == on_sunday
        frozen_time.move_to(today.replace(day=8))
        assert await f(FakePR({"foo": date.DayOfWeek(7)})) == next_saturday
        frozen_time.move_to(today.replace(day=2))
        assert await f(FakePR({"foo": date.DayOfWeek(1)})) == on_saturday


async def test_month_near_datetime() -> None:
    with freeze_time("2012-06-06T12:15:00", tz_offset=0) as frozen_time:
        today = frozen_time().replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=datetime.timezone.utc
        )
        in_june = today.replace(month=today.month, day=1)
        in_july = today.replace(month=today.month + 1, day=1)
        next_year_in_january = today.replace(year=today.year + 1, month=1, day=1)
        next_year_in_june = in_june.replace(year=in_june.year + 1, month=6, day=1)

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == in_july
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june

        f = filter.NearDatetimeFilter({"<": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == in_july
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june

        f = filter.NearDatetimeFilter({"<": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_january
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june

        f = filter.NearDatetimeFilter({"=": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == in_july
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_june
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june

        f = filter.NearDatetimeFilter({"!=": ("foo", date.Month(6))})
        frozen_time.move_to(in_june.replace(month=6))
        assert await f(FakePR({"foo": date.Month(6)})) == in_july
        frozen_time.move_to(in_june.replace(month=7))
        assert await f(FakePR({"foo": date.Month(7)})) == next_year_in_june
        frozen_time.move_to(in_june.replace(month=1))
        assert await f(FakePR({"foo": date.Month(1)})) == in_june


async def test_year_near_datetime() -> None:
    with freeze_time("2012-01-14T12:15:00", tz_offset=0) as frozen_time:
        today = frozen_time().replace(tzinfo=datetime.timezone.utc)
        in_2016 = datetime.datetime(
            2016, 1, 1, 0, 0, 0, 0, tzinfo=datetime.timezone.utc
        )
        in_2017 = datetime.datetime(
            2017, 1, 1, 0, 0, 0, 0, tzinfo=datetime.timezone.utc
        )

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == in_2017
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016

        f = filter.NearDatetimeFilter({"<": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016

        f = filter.NearDatetimeFilter({"<=": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == in_2017
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016

        f = filter.NearDatetimeFilter({"<": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016

        f = filter.NearDatetimeFilter({"=": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == in_2017
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016

        f = filter.NearDatetimeFilter({"!=": ("foo", date.Year(2016))})
        frozen_time.move_to(today.replace(year=2016))
        assert await f(FakePR({"foo": date.Year(2016)})) == in_2017
        frozen_time.move_to(today.replace(year=2017))
        assert await f(FakePR({"foo": date.Year(2017)})) == filter.DT_MAX
        frozen_time.move_to(today.replace(year=2011))
        assert await f(FakePR({"foo": date.Year(2011)})) == in_2016


async def test_time_near_datetime() -> None:
    with freeze_time("2012-01-06T05:08:00", tz_offset=0) as frozen_time:
        now = frozen_time().replace(tzinfo=datetime.timezone.utc)
        atmidnight = now.replace(
            day=now.day + 1,
            hour=0,
            second=0,
            minute=0,
            microsecond=0,
        )
        nextday = now.replace(day=now.day + 1)
        soon = now.replace(minute=now.minute + 1)

        f = filter.NearDatetimeFilter({"<=": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == soon
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight

        f = filter.NearDatetimeFilter({"<": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight

        f = filter.NearDatetimeFilter({">=": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == soon
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight

        f = filter.NearDatetimeFilter({">": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == atmidnight

        f = filter.NearDatetimeFilter({"=": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == soon
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == nextday
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == nextday

        f = filter.NearDatetimeFilter({"!=": ("foo", now.timetz())})
        frozen_time.move_to(now)
        assert await f(FakePR({"foo": frozen_time().timetz()})) == soon
        frozen_time.move_to(now.replace(hour=2, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=5, minute=1))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == now
        frozen_time.move_to(now.replace(hour=6, minute=2))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == nextday
        frozen_time.move_to(now.replace(hour=8, minute=9))
        assert await f(FakePR({"foo": frozen_time().timetz()})) == nextday


@freeze_time("2012-01-14T12:15:00")
async def test_datetime_near_datetime() -> None:
    today = datetime.datetime(2012, 1, 14, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    soon = datetime.datetime(2012, 1, 14, 5, 9, 0, 0, tzinfo=datetime.timezone.utc)

    f = filter.NearDatetimeFilter({"<=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX

    f = filter.NearDatetimeFilter({"<": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX

    f = filter.NearDatetimeFilter({">=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX

    f = filter.NearDatetimeFilter({">": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX

    f = filter.NearDatetimeFilter({"=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX

    f = filter.NearDatetimeFilter({"!=": ("foo", today)})
    assert await f(FakePR({"foo": dtime(5, 8)})) == soon
    assert await f(FakePR({"foo": dtime(2, 1)})) == today
    assert await f(FakePR({"foo": dtime(5, 1)})) == today
    assert await f(FakePR({"foo": dtime(6, 2)})) == filter.DT_MAX
    assert await f(FakePR({"foo": dtime(8, 9)})) == filter.DT_MAX


@freeze_time("2012-01-14T12:05:00")
async def test_multiple_near_datetime() -> None:
    atmidnight = datetime.datetime(
        2012, 1, 15, 0, 0, 0, 0, tzinfo=datetime.timezone.utc
    )
    today = datetime.datetime(2012, 1, 14, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    in_two_hours = datetime.datetime(
        2012, 1, 14, 7, 5, 0, 0, tzinfo=datetime.timezone.utc
    )
    soon = datetime.datetime(2012, 1, 14, 5, 9, 0, 0, tzinfo=datetime.timezone.utc)

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
    assert await f(FakePR({"foo": time(8, 9)})) == atmidnight
    assert await f(FakePR({"foo": time(18, 9)})) == atmidnight

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
    assert await f(FakePR({"foo": time(8, 9)})) == atmidnight
    assert await f(FakePR({"foo": time(18, 9)})) == atmidnight

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
    assert await f(FakePR({"foo": time(8, 9)})) == atmidnight
    assert await f(FakePR({"foo": time(18, 9)})) == atmidnight

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
    assert await f(FakePR({"foo": time(8, 9)})) == atmidnight
    assert await f(FakePR({"foo": time(18, 9)})) == atmidnight


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
