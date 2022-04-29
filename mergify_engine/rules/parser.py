# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
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
import enum
import re
import typing

from mergify_engine import date
from mergify_engine.rules import filter


@dataclasses.dataclass
class ConditionParsingError(Exception):
    message: str


class Parser(enum.Enum):
    TEXT = enum.auto()
    WORD = enum.auto()
    BRANCH = enum.auto()
    LOGIN_AND_TEAMS = enum.auto()
    POSITIVE_NUMBER = enum.auto()
    NUMBER = enum.auto()
    TIME = enum.auto()
    DAY = enum.auto()
    MONTH = enum.auto()
    YEAR = enum.auto()
    DOW = enum.auto()
    SCHEDULE = enum.auto()
    TIMESTAMP_OR_TIMEDELTA = enum.auto()
    TIMESTAMP = enum.auto()
    BOOL = enum.auto()


CONDITION_PARSERS = {
    "number": Parser.POSITIVE_NUMBER,
    "head": Parser.BRANCH,
    "base": Parser.BRANCH,
    "author": Parser.LOGIN_AND_TEAMS,
    "merged-by": Parser.LOGIN_AND_TEAMS,
    "body": Parser.TEXT,
    "body-raw": Parser.TEXT,
    "assignee": Parser.LOGIN_AND_TEAMS,
    "label": Parser.TEXT,
    "title": Parser.TEXT,
    "files": Parser.TEXT,
    "commits-behind": Parser.TEXT,
    "commits": Parser.TEXT,
    "milestone": Parser.WORD,
    "queue-position": Parser.NUMBER,
    "review-requested": Parser.LOGIN_AND_TEAMS,
    "approved-reviews-by": Parser.LOGIN_AND_TEAMS,
    "dismissed-reviews-by": Parser.LOGIN_AND_TEAMS,
    "changes-requested-reviews-by": Parser.LOGIN_AND_TEAMS,
    "commented-reviews-by": Parser.LOGIN_AND_TEAMS,
    "status-success": Parser.TEXT,
    "status-failure": Parser.TEXT,
    "status-neutral": Parser.TEXT,
    "check-success": Parser.TEXT,
    "check-success-or-neutral": Parser.TEXT,
    "check-failure": Parser.TEXT,
    "check-neutral": Parser.TEXT,
    "check-skipped": Parser.TEXT,
    "check-pending": Parser.TEXT,
    "check-stale": Parser.TEXT,
    "commits-unverified": Parser.TEXT,
    "review-threads-resolved": Parser.TEXT,
    "review-threads-unresolved": Parser.TEXT,
    "repository-name": Parser.TEXT,
    "repository-full-name": Parser.TEXT,
    "current-time": Parser.TIME,
    "current-day": Parser.DAY,
    "current-month": Parser.MONTH,
    "current-year": Parser.YEAR,
    "current-day-of-week": Parser.DOW,
    "schedule": Parser.SCHEDULE,
    "created-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "updated-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "closed-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "merged-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "queued-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "queue-merge-started-at": Parser.TIMESTAMP_OR_TIMEDELTA,
    "current-timestamp": Parser.TIMESTAMP,
    "locked": Parser.BOOL,
    "merged": Parser.BOOL,
    "closed": Parser.BOOL,
    "conflict": Parser.BOOL,
    "draft": Parser.BOOL,
    "linear-history": Parser.BOOL,
}
# NOTE(sileht): From the longest string to the short one to ensure for
# example that merged-at is selected before merged
ATTRIBUTES = sorted(CONDITION_PARSERS, key=lambda v: (len(v), v), reverse=True)


ATTRIBUTES_WITH_ONLY_LENGTH = ("commits-behind",)

# Negate, quantity (default: True, True)
PARSER_MODIFIERS = {
    Parser.BOOL: (True, False),
    Parser.SCHEDULE: (False, False),
    Parser.TIME: (False, False),
    Parser.TIMESTAMP: (False, False),
    Parser.TIMESTAMP_OR_TIMEDELTA: (False, False),
    Parser.DOW: (False, False),
    Parser.NUMBER: (True, False),
    Parser.POSITIVE_NUMBER: (True, False),
    Parser.DAY: (False, False),
    Parser.MONTH: (False, False),
    Parser.YEAR: (False, False),
}

NEGATION_OPERATORS = ("-", "¬")
POSITIVE_OPERATORS = ("+",)
RANGE_OPERATORS = (">=", "<=", "≥", "≤", "<", ">")
EQUALITY_OPERATORS = ("==", "!=", "≠", "=", ":")
OPERATOR_ALIASES = {
    ":": "=",
    "==": "=",
    "≠": "!=",
    "≥": ">=",
    "≤": "<=",
}
REGEX_OPERATOR = "~="
SIMPLE_OPERATORS = EQUALITY_OPERATORS + RANGE_OPERATORS
ALL_OPERATORS = SIMPLE_OPERATORS + (REGEX_OPERATOR,)


SUPPORTED_OPERATORS = {
    Parser.TEXT: ALL_OPERATORS,
    Parser.WORD: ALL_OPERATORS,
    Parser.BRANCH: ALL_OPERATORS,
    Parser.LOGIN_AND_TEAMS: ALL_OPERATORS,
    Parser.SCHEDULE: EQUALITY_OPERATORS,
    Parser.TIME: RANGE_OPERATORS,
    Parser.TIMESTAMP: RANGE_OPERATORS,
    Parser.TIMESTAMP_OR_TIMEDELTA: RANGE_OPERATORS,
    Parser.NUMBER: SIMPLE_OPERATORS,
    Parser.POSITIVE_NUMBER: SIMPLE_OPERATORS,
    Parser.DAY: SIMPLE_OPERATORS,
    Parser.MONTH: SIMPLE_OPERATORS,
    Parser.YEAR: SIMPLE_OPERATORS,
    Parser.DOW: SIMPLE_OPERATORS,
}

INVALID_BRANCH_CHARS = "~^: []\\"

ALPHASNUMS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

GITHUB_LOGIN_CHARS = ALPHASNUMS + "-[]"
GITHUB_LOGIN_CHARS = GITHUB_LOGIN_CHARS + "@/"


def _to_dict(
    negate: bool,
    quantity: bool,
    attribute: str,
    operator: str,
    value: typing.Any,
) -> filter.TreeT:
    if quantity:
        attribute = f"#{attribute}"
    d = typing.cast(filter.TreeT, {operator: (attribute, value)})
    if negate:
        return filter.TreeT({"-": d})
    return d


def _unquote(value: str) -> str:
    if not value:
        return value
    elif (
        (value[0] == "'" and value[-1] != "'")
        or (value[0] == '"' and value[-1] != '"')
        or (value[0] != "'" and value[-1] == "'")
        or (value[0] != '"' and value[-1] == '"')
    ):
        raise ConditionParsingError("Unbalanced quotes")
    elif (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
        and len(value) >= 2
    ):
        value = value[1:-1]
    return value


def _extract_date(
    date_type: typing.Type[date.PartialDatetime], value: str
) -> date.PartialDatetime:
    try:
        return date_type.from_string(value)
    except date.InvalidDate as e:
        raise ConditionParsingError(e.message)


def _extract_time(value: str) -> date.Time:
    try:
        return date.Time.from_string(value)
    except date.InvalidDate as e:
        raise ConditionParsingError(e.message)


def _extract_dow_range(days: str) -> typing.Dict[str, typing.Any]:
    dow1_str, sep, dow2_str = days.partition("-")
    if sep != "-":
        raise ConditionParsingError(
            f"Invalid schedule: {days} -> {dow1_str} {sep} {dow2_str}"
        )

    dow1 = _extract_date(date.DayOfWeek, dow1_str)
    dow2 = _extract_date(date.DayOfWeek, dow2_str)
    return {
        "and": (
            {">=": ("current-day-of-week", dow1)},
            {"<=": ("current-day-of-week", dow2)},
        )
    }


def _extract_time_range(times: str) -> typing.Dict[str, typing.Any]:
    time1_str, sep, time2_str = times.partition("-")
    if sep != "-":
        raise ConditionParsingError("Invalid schedule")
    time1 = _extract_time(time1_str)
    time2 = _extract_time(time2_str)

    # NOTE(sileht): In case of the format is `10:00-18:00[Europe/Paris]`,
    # we assume the first time is also [Europe/Paris]
    if time1.tzinfo == datetime.timezone.utc and time1.tzinfo != time2.tzinfo:
        time1.tzinfo = time2.tzinfo

    return {
        "and": (
            {">=": ("current-time", time1)},
            {"<=": ("current-time", time2)},
        )
    }


def _skip_ws(v: str, lenght: int, position: int) -> int:
    while position < lenght and v[position] == " ":
        position += 1
    return position


def parse(v: str) -> typing.Any:
    length = len(v)
    position = _skip_ws(v, length, 0)
    if position >= length:
        raise ConditionParsingError("Condition empty")

    # Search for modifiers
    negate = False
    quantity = False
    if v[position] in NEGATION_OPERATORS:
        negate = True
        position += 1
    elif v[position] in POSITIVE_OPERATORS:
        position += 1

    position = _skip_ws(v, length, position)
    if position >= length:
        raise ConditionParsingError("Incomplete condition")

    if v[position] == "#":
        quantity = True
        position = _skip_ws(v, length, position + 1)
        if position >= length:
            raise ConditionParsingError("Incomplete condition")

    # Get the attribute
    for attribute in ATTRIBUTES:
        if v[position:].startswith(attribute):
            break
    else:
        raise ConditionParsingError("Invalid attribute")

    position = _skip_ws(v, length, position + len(attribute))

    if not quantity and attribute in ATTRIBUTES_WITH_ONLY_LENGTH:
        raise ConditionParsingError(
            f"`#` modifier is required for attribute: `{attribute}`"
        )
    # Get the type of parser
    parser = CONDITION_PARSERS[attribute]

    # Check modifiers
    negate_allowed, quantity_allowed = PARSER_MODIFIERS.get(parser, (True, True))
    if negate and not negate_allowed:
        raise ConditionParsingError(
            f"`-` modifier is invalid for attribute: `{attribute}`"
        )
    if quantity and not quantity_allowed:
        raise ConditionParsingError(
            f"`#` modifier is invalid for attribute: `{attribute}`"
        )

    # Bool doesn't have operators
    if parser == Parser.BOOL:
        if len(v[position:].strip()) > 0:
            raise ConditionParsingError(
                f"Operators are invalid for Boolean attribute: `{attribute}`"
            )
        return _to_dict(negate, False, attribute, "=", True)

    # Extract operators
    operators = SUPPORTED_OPERATORS[parser]
    for op in operators:
        if v[position:].startswith(op):
            break
    else:
        raise ConditionParsingError("Invalid operator")
    position += len(op)
    value = v[position:].strip()
    op = OPERATOR_ALIASES.get(op, op)

    if parser == Parser.SCHEDULE:
        value = _unquote(value)
        if op == "!=":
            negate = True
        cond: typing.Dict[str, typing.Any]
        days, has_times, times = value.partition(" ")
        try:
            dow_cond = _extract_dow_range(days)
        except ConditionParsingError:
            if has_times:
                raise
            cond = _extract_time_range(days)
        else:
            if has_times:
                time_cond = _extract_time_range(times)
                cond = {"and": (dow_cond, time_cond)}
            else:
                cond = dow_cond
        return _to_dict(negate, False, attribute, "@", cond)

    elif parser == Parser.TIME:
        value = _unquote(value)
        t = _extract_time(value)
        return _to_dict(False, False, attribute, op, t)

    elif parser in (Parser.TIMESTAMP, Parser.TIMESTAMP_OR_TIMEDELTA):
        value = _unquote(value)
        if parser == Parser.TIMESTAMP_OR_TIMEDELTA:
            try:
                rd = date.RelativeDatetime.from_string(value)
            except date.InvalidDate:
                pass
            else:
                return _to_dict(False, False, f"{attribute}-relative", op, rd)

        try:
            d = date.fromisoformat_with_zoneinfo(value)
        except date.InvalidDate as e:
            raise ConditionParsingError(e.message)
        return _to_dict(False, False, attribute, op, d)

    elif parser in (
        Parser.NUMBER,
        Parser.POSITIVE_NUMBER,
    ):
        try:
            number = int(value)
        except ValueError:
            raise ConditionParsingError(f"{value} is not a number")

        if parser == Parser.POSITIVE_NUMBER and number < 0:
            raise ConditionParsingError("Value must be positive")
        return _to_dict(negate, False, attribute, op, number)

    elif parser in (
        Parser.DAY,
        Parser.MONTH,
        Parser.YEAR,
        Parser.DOW,
    ):
        pd: date.PartialDatetime
        if parser == Parser.DOW:
            pd = _extract_date(date.DayOfWeek, value)
        elif parser == Parser.DAY:
            pd = _extract_date(date.Day, value)
        elif parser == Parser.MONTH:
            pd = _extract_date(date.Month, value)
        elif parser == Parser.YEAR:
            pd = _extract_date(date.Year, value)
        else:
            raise RuntimeError("unhandled date parser")
        return _to_dict(negate, False, attribute, op, pd)

    elif parser in (
        Parser.TEXT,
        Parser.WORD,
        Parser.BRANCH,
        Parser.LOGIN_AND_TEAMS,
    ):
        if (
            parser == Parser.LOGIN_AND_TEAMS
            and value
            and value[0] == "@"
            and op not in SIMPLE_OPERATORS
        ):
            raise ConditionParsingError(
                "Regular expression are not supported for team slug"
            )

        if quantity:
            try:
                number = int(value)
            except ValueError:
                raise ConditionParsingError(f"{value} is not a number")
            return _to_dict(negate, True, attribute, op, number)

        if op == REGEX_OPERATOR:
            try:
                # TODO(sileht): we can keep the compiled version, so the
                # Filter() doesn't have to (re)compile it.
                re.compile(value)
            except re.error as e:
                raise ConditionParsingError(f"Invalid regular expression: {str(e)}")
            return _to_dict(negate, quantity, attribute, op, value)

        if parser == Parser.TEXT:
            value = _unquote(value)
        elif parser == Parser.WORD:
            if " " in value:
                raise ConditionParsingError(f"Invalid `{attribute}` format")
        elif parser == Parser.BRANCH:
            for char in INVALID_BRANCH_CHARS:
                if char in value:
                    raise ConditionParsingError("Invalid branch name")
        elif parser == Parser.LOGIN_AND_TEAMS:
            if value and value[0] == "@":
                if value.count("@") > 1 or value.count("/") > 1:
                    raise ConditionParsingError("Invalid team name")
                valid_chars = GITHUB_LOGIN_CHARS
            else:
                valid_chars = GITHUB_LOGIN_CHARS
            for char in value:
                if char not in valid_chars:
                    if value and value[0] == "@":
                        raise ConditionParsingError("Invalid GitHub team name")
                    else:
                        raise ConditionParsingError("Invalid GitHub login")
        return _to_dict(negate, quantity, attribute, op, value)
    else:
        raise RuntimeError(f"unhandled parser: {parser}")
