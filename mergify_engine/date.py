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
import typing
import zoneinfo


DT_MAX = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


@dataclasses.dataclass(order=True)
class PartialDatetime:
    value: int

    def __str__(self) -> str:
        return str(self.value)


@dataclasses.dataclass(order=True)
class RelativeDatetime:
    # NOTE(sileht): Like a datetime, but we known it has been computed from `utcnow() + timedelta()`
    value: datetime.datetime


@dataclasses.dataclass
class Year(PartialDatetime):
    pass


@dataclasses.dataclass
class Month(PartialDatetime):
    pass


@dataclasses.dataclass
class Day(PartialDatetime):
    pass


@functools.total_ordering
@dataclasses.dataclass
class Time:
    hour: int
    minute: int
    tzinfo: datetime.tzinfo

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
            return ref.replace(minute=obj.minute, hour=obj.hour, tzinfo=obj.tzinfo)
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
            return cls(cls._LONG_DAY.index(string) + 1)
        except ValueError:
            pass
        return cls(int(string))

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


def fromtimestamp(timestamp: float) -> datetime.datetime:
    """always returns an aware datetime object with UTC timezone"""
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def pretty_datetime(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")
