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
import dataclasses
import datetime
import functools
import re
import typing
import zoneinfo


DT_MAX = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


@dataclasses.dataclass
class InvalidDate(Exception):
    message: str


TIMEZONES = {f"[{tz}]" for tz in zoneinfo.available_timezones()}


def extract_timezone(
    value: str,
) -> typing.Tuple[str, typing.Union[datetime.timezone, zoneinfo.ZoneInfo]]:
    if value[-1] == "]":
        for timezone in TIMEZONES:
            if value.endswith(timezone):
                return value[: -len(timezone)], zoneinfo.ZoneInfo(timezone[1:-1])
        raise InvalidDate("Invalid timezone")
    return value, datetime.timezone.utc


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


@dataclasses.dataclass(order=True)
class PartialDatetime:
    value: int

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def from_string(cls, value: str) -> "PartialDatetime":
        try:
            number = int(value)
        except ValueError:
            raise InvalidDate(f"{value} is not a number")
        return cls(number)


class TimedeltaRegexResultT(typing.TypedDict):
    filled: typing.Optional[str]
    days: typing.Optional[str]
    hours: typing.Optional[str]
    minutes: typing.Optional[str]
    seconds: typing.Optional[str]


@dataclasses.dataclass(order=True)
class RelativeDatetime:
    # NOTE(sileht): Like a datetime, but we known it has been computed from `utcnow() + timedelta()`
    value: datetime.datetime

    # PostgreSQL's day-time interval format without seconds and microseconds, e.g. "3 days 04:05"
    _TIMEDELTA_TO_NOW_RE: typing.ClassVar[typing.Pattern[str]] = re.compile(
        r"^"
        r"(?:(?P<days>\d+) (days? ?))?"
        r"(?:"
        r"(?P<hours>\d+):"
        r"(?P<minutes>\d\d)"
        r")? ago$"
    )

    @classmethod
    def from_string(cls, value: str) -> "RelativeDatetime":
        m = cls._TIMEDELTA_TO_NOW_RE.match(value)
        if m is None:
            raise InvalidDate("Invalid relative date")

        kw = typing.cast(TimedeltaRegexResultT, m.groupdict())
        return cls(
            utcnow()
            - datetime.timedelta(
                days=int(kw["days"] or 0),
                hours=int(kw["hours"] or 0),
                minutes=int(kw["minutes"] or 0),
            )
        )

    def __post_init__(self) -> None:
        if self.value.tzinfo is None:
            raise InvalidDate("timezone is missing")


@dataclasses.dataclass
class Year(PartialDatetime):
    def __post_init__(self) -> None:
        if self.value < 2000 or self.value > 9999:
            raise InvalidDate("Year must be between 2000 and 9999")


@dataclasses.dataclass
class Month(PartialDatetime):
    def __post_init__(self) -> None:
        if self.value < 1 or self.value > 12:
            raise InvalidDate("Month must be between 1 and 12")


@dataclasses.dataclass
class Day(PartialDatetime):
    def __post_init__(self) -> None:
        if self.value < 1 or self.value > 31:
            raise InvalidDate("Day must be between 1 and 31")


@functools.total_ordering
@dataclasses.dataclass
class Time:
    hour: int
    minute: int
    tzinfo: datetime.tzinfo

    @classmethod
    def from_string(cls, string: str) -> "Time":
        value, tzinfo = extract_timezone(string)
        hour_str, sep, minute_str = value.partition(":")
        if sep != ":":
            raise InvalidDate("Invalid time")
        try:
            hour = int(hour_str)
        except ValueError:
            raise InvalidDate(f"{hour_str} is not a number")
        try:
            minute = int(minute_str)
        except ValueError:
            raise InvalidDate(f"{minute_str} is not a number")

        return cls(hour=hour, minute=minute, tzinfo=tzinfo)

    def __post_init__(self) -> None:
        if self.hour < 0 or self.hour >= 24:
            raise InvalidDate("Hour must be between 0 and 23")
        elif self.minute < 0 or self.minute >= 60:
            raise InvalidDate("Minute must be between 0 and 59")

    def __str__(self) -> str:
        value = f"{self.hour:02d}:{self.minute:02d}"
        if isinstance(self.tzinfo, zoneinfo.ZoneInfo):
            value += f"[{self.tzinfo.key}]"
        return value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (Time, datetime.datetime)):
            raise ValueError(f"Unsupport comparaison type: {type(other)}")
        now = utcnow()
        d1 = self._to_dt(self, now)
        d2 = self._to_dt(other, now)
        return d1 == d2

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, (Time, datetime.datetime)):
            raise ValueError(f"Unsupport comparaison type: {type(other)}")
        now = utcnow()
        d1 = self._to_dt(self, now)
        d2 = self._to_dt(other, now)
        return d1 > d2

    @staticmethod
    def _to_dt(
        obj: typing.Union["Time", datetime.datetime], ref: datetime.datetime
    ) -> datetime.datetime:
        if isinstance(obj, datetime.datetime):
            return obj
        elif isinstance(obj, Time):
            return ref.astimezone(obj.tzinfo).replace(
                minute=obj.minute,
                hour=obj.hour,
                second=0,
                microsecond=0,
            )
        else:
            raise ValueError(f"Unsupport comparaison type: {type(obj)}")


@dataclasses.dataclass
class DayOfWeek(PartialDatetime):
    _SHORT_DAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    _LONG_DAY = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )

    @classmethod
    def from_string(cls, string: str) -> "DayOfWeek":
        try:
            return cls(cls._SHORT_DAY.index(string.lower()) + 1)
        except ValueError:
            pass
        try:
            return cls(cls._LONG_DAY.index(string.lower()) + 1)
        except ValueError:
            pass
        try:
            dow = int(string)
        except ValueError:
            raise InvalidDate(f"{string} is not a number or literal day of the week")
        return cls(dow)

    def __post_init__(self) -> None:
        if self.value < 1 or self.value > 7:
            raise InvalidDate("Day of the week must be between 1 and 7")

    def __str__(self) -> str:
        return self._SHORT_DAY[self.value - 1].capitalize()


def fromisoformat(s: str) -> datetime.datetime:
    """always returns an aware datetime object with UTC timezone"""
    if s[-1] == "Z":
        s = s[:-1]
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    else:
        return dt.astimezone(datetime.timezone.utc)


def fromisoformat_with_zoneinfo(string: str) -> datetime.datetime:
    value, tzinfo = extract_timezone(string)
    try:
        # TODO(sileht): astimezone doesn't look logic, but keep the
        # same behavior as the old parse for now
        return fromisoformat(value).astimezone(tzinfo)
    except ValueError:
        raise InvalidDate("Invalid timestamp")


def fromtimestamp(timestamp: float) -> datetime.datetime:
    """always returns an aware datetime object with UTC timezone"""
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def pretty_datetime(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def pretty_time(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M %Z")


_INTERVAL_RE = re.compile(
    r"""
    (?P<filled>
        ((?P<days>[-+]?\d+)\s*d(ays?)? \s* )?
        ((?P<hours>[-+]?\d+)\s*h(ours?)? \s* )?
        ((?P<minutes>[-+]?\d+)\s*m((inutes?|ins?)?)? \s* )?
        ((?P<seconds>[-+]?\d+)\s*s(econds?)? \s* )?
    )
    """,
    re.VERBOSE,
)


def interval_from_string(value: str) -> datetime.timedelta:
    m = _INTERVAL_RE.match(value)
    if m is None:
        raise InvalidDate("Invalid date interval")

    kw = typing.cast(TimedeltaRegexResultT, m.groupdict())
    if not kw or not kw["filled"]:
        raise InvalidDate("Invalid date interval")

    return datetime.timedelta(
        days=int(kw["days"] or 0),
        hours=int(kw["hours"] or 0),
        minutes=int(kw["minutes"] or 0),
        seconds=int(kw["seconds"] or 0),
    )
