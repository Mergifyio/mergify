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


DT_MAX = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


@dataclasses.dataclass(order=True)
class PartialDatetime:
    value: int

    def __str__(self) -> str:
        return str(self.value)


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
    if s[-1] == "Z":
        s = s[:-1]
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    else:
        return dt.astimezone(datetime.timezone.utc)
