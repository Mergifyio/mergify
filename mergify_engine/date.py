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
import typing


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


def _pretty_join(strings: typing.List[str]) -> str:
    if len(strings) == 0:
        return ""
    elif len(strings) == 1:
        return strings[0]
    else:
        return f"{', '.join(strings[:-1])} and {strings[-1]}"


def _number_with_unit(n: int, unit: str) -> str:
    if n >= -1 and n <= 1:
        return f"{n} {unit}"
    return f"{n} {unit}s"


def pretty_timedelta(t: datetime.timedelta) -> str:
    seconds = int(t.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(_number_with_unit(days, "day"))
    if days > 0 or hours > 0:
        parts.append(_number_with_unit(hours, "hour"))
    if days > 0 or hours > 0 or minutes > 0:
        parts.append(_number_with_unit(minutes, "minute"))

    if days == 0 and hours == 0 and minutes == 0:
        parts.append(_number_with_unit(seconds, "second"))

    return _pretty_join(parts)
