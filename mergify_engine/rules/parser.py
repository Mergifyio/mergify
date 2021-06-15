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
import datetime
import re
import typing

import pyparsing

from mergify_engine import date
from mergify_engine import utils


git_branch = pyparsing.CharsNotIn("~^: []\\")
regexp = pyparsing.CharsNotIn("")
integer = pyparsing.Word(pyparsing.nums).setParseAction(lambda toks: int(toks[0]))
github_login = pyparsing.Word(pyparsing.alphanums + "-[]")
github_team = pyparsing.Combine(
    pyparsing.Literal("@") + github_login + pyparsing.Literal("/") + github_login
) | pyparsing.Combine(pyparsing.Literal("@") + github_login)
text = (
    pyparsing.QuotedString('"') | pyparsing.QuotedString("'") | pyparsing.CharsNotIn("")
)
milestone = pyparsing.CharsNotIn(" ")

_match_time = (
    pyparsing.Word(pyparsing.nums).addCondition(
        lambda tokens: int(tokens[0]) >= 0 and int(tokens[0]) < 24
    )
    + pyparsing.Literal(":")
    + pyparsing.Word(pyparsing.nums).addCondition(
        lambda tokens: int(tokens[0]) >= 0 and int(tokens[0]) < 60
    )
).setParseAction(
    lambda toks: datetime.time(
        hour=int(toks[0]), minute=int(toks[2]), tzinfo=datetime.timezone.utc
    )
)

_day = (
    pyparsing.Word(pyparsing.nums)
    .setParseAction(lambda tokens: date.Day(int(tokens[0])))
    .addCondition(
        lambda tokens: tokens[0].value >= 1 and tokens[0].value <= 31,
        message="day must be between 1 and 31",
    )
)
_month = (
    pyparsing.Word(pyparsing.nums)
    .setParseAction(lambda tokens: date.Month(int(tokens[0])))
    .addCondition(
        lambda tokens: tokens[0].value >= 1 and tokens[0].value <= 12,
        message="month must be between 1 and 12",
    )
)
_year = (
    pyparsing.Word(pyparsing.nums)
    .setParseAction(lambda tokens: date.Year(int(tokens[0])))
    .addCondition(
        lambda tokens: tokens[0].value >= 2000 and tokens[0].value <= 9999,
        message="year must be between 2000 and 9999",
    )
)
_day_of_week_str = (
    pyparsing.Word(pyparsing.nums)
    | pyparsing.CaselessLiteral("monday")
    | pyparsing.CaselessLiteral("tuesday")
    | pyparsing.CaselessLiteral("wednesday")
    | pyparsing.CaselessLiteral("thursday")
    | pyparsing.CaselessLiteral("friday")
    | pyparsing.CaselessLiteral("saturday")
    | pyparsing.CaselessLiteral("sunday")
    | pyparsing.CaselessLiteral("mon")
    | pyparsing.CaselessLiteral("tue")
    | pyparsing.CaselessLiteral("wed")
    | pyparsing.CaselessLiteral("thu")
    | pyparsing.CaselessLiteral("fri")
    | pyparsing.CaselessLiteral("sat")
    | pyparsing.CaselessLiteral("sun")
)
_day_of_week = _day_of_week_str.setParseAction(
    lambda tokens: date.DayOfWeek.from_string(tokens[0])
).addCondition(
    lambda tokens: tokens[0].value >= 1 and tokens[0].value <= 7,
    message="day-of-week must be between 1 and 7",
)


# PostgreSQL's day-time interval format without seconds and microseconds, e.g. "3 days 04:05"
_interval_re = re.compile(
    r"^"
    r"(?:(?P<days>-?\d+) (days? ?))?"
    r"(?:(?P<sign>[-+])?"
    r"(?P<hours>\d+):"
    r"(?P<minutes>\d\d)"
    r")?$"
)


def _interval_result_to_dt(tokens):
    m = _interval_re.match(tokens[0])
    if m is None:
        raise pyparsing.ParseException("invalid time interval")
    kw = m.groupdict()
    days = datetime.timedelta(float(kw.pop("days", 0) or 0))
    sign = -1 if kw.pop("sign", "+") == "-" else 1
    kw = {k: float(v) for k, v in kw.items() if v is not None}
    return utils.utcnow() - (days + sign * datetime.timedelta(**kw))


interval = text.copy().setParseAction(_interval_result_to_dt)


def _iso_datetime(tokens):
    try:
        return date.fromisoformat(tokens[0])
    except ValueError:
        raise pyparsing.ParseException("invalid datetime")


iso_datetime = text.copy().setParseAction(_iso_datetime)

_day_of_week_range = (
    _day_of_week + pyparsing.Literal("-") + _day_of_week
).setParseAction(
    lambda toks: {
        "and": (
            {">=": ("current-day-of-week", toks[0])},
            {"<=": ("current-day-of-week", toks[2])},
        )
    }
)
_time_range = (_match_time + pyparsing.Literal("-") + _match_time).setParseAction(
    lambda toks: {
        "and": (
            {">=": ("current-time", toks[0])},
            {"<=": ("current-time", toks[2])},
        )
    }
)
_schedule = (_day_of_week_range + pyparsing.White(" ") + _time_range).setParseAction(
    lambda toks: {"and": (toks[0], toks[2])}
)
_schedule = _schedule | _time_range | _day_of_week_range


def convert_equality_to_at(toks):
    not_ = toks[1] == "!="
    toks[1] = "@"
    if not_:
        return {"-": toks}
    else:
        return toks


regex_operators = pyparsing.Literal("~=")


equality_operators = (
    pyparsing.Literal(":").setParseAction(pyparsing.replaceWith("="))
    | pyparsing.Literal("=")
    | pyparsing.Literal("==").setParseAction(pyparsing.replaceWith("="))
    | pyparsing.Literal("!=")
    | pyparsing.Literal("≠").setParseAction(pyparsing.replaceWith("!="))
)

range_operators = (
    pyparsing.Literal(">=")
    | pyparsing.Literal("≥").setParseAction(pyparsing.replaceWith(">="))
    | pyparsing.Literal("<=")
    | pyparsing.Literal("≤").setParseAction(pyparsing.replaceWith("<="))
    | pyparsing.Literal("<")
    | pyparsing.Literal(">")
)
simple_operators = equality_operators | range_operators


def _match_boolean(literal: str) -> pyparsing.Token:
    return (
        literal
        + pyparsing.Empty().setParseAction(pyparsing.replaceWith("="))
        + pyparsing.Empty().setParseAction(pyparsing.replaceWith(True))
    )


match_integer = simple_operators + integer


def _match_with_operator(token: pyparsing.Token) -> pyparsing.Token:
    return (simple_operators + token) | (regex_operators + regexp)


def _token_to_dict(
    s: str, loc: int, toks: typing.List[pyparsing.Token]
) -> typing.Dict[str, typing.Any]:
    if len(toks) == 3:
        # datetime attributes
        key_op = ""
        not_ = False
        key, op, value = toks
    elif len(toks) == 5:
        # quantifiable_attributes
        not_, key_op, key, op, value = toks
    elif len(toks) == 4:
        # non_quantifiable_attributes
        key_op = ""
        not_, key, op, value = toks
    else:
        raise RuntimeError("unexpected search parser format")

    if key_op == "#":
        value = int(value)
    d = {op: (key_op + key, value)}
    if not_:
        return {"-": d}
    return d


_match_login_or_teams = _match_with_operator(github_login) | (
    simple_operators + github_team
)

head = "head" + _match_with_operator(git_branch)
base = "base" + _match_with_operator(git_branch)
author = "author" + _match_login_or_teams
merged_by = "merged-by" + _match_login_or_teams
body = "body" + _match_with_operator(text)
assignee = "assignee" + _match_login_or_teams
label = "label" + _match_with_operator(text)
title = "title" + _match_with_operator(text)
files = "files" + _match_with_operator(text)
milestone = "milestone" + _match_with_operator(milestone)
number = "number" + match_integer
review_requests = "review-requested" + _match_login_or_teams
review_approved_by = "approved-reviews-by" + _match_login_or_teams
review_dismissed_by = "dismissed-reviews-by" + _match_login_or_teams
review_changes_requested_by = "changes-requested-reviews-by" + _match_login_or_teams
review_commented_by = "commented-reviews-by" + _match_login_or_teams
status_success = "status-success" + _match_with_operator(text)
status_failure = "status-failure" + _match_with_operator(text)
status_neutral = "status-neutral" + _match_with_operator(text)
check_success = "check-success" + _match_with_operator(text)
check_success_or_neutral = "check-success-or-neutral" + _match_with_operator(text)
check_failure = "check-failure" + _match_with_operator(text)
check_neutral = "check-neutral" + _match_with_operator(text)
check_skipped = "check-skipped" + _match_with_operator(text)
check_pending = "check-pending" + _match_with_operator(text)
check_stale = "check-stale" + _match_with_operator(text)
current_time = "current-time" + range_operators + _match_time
current_day = "current-day" + _match_with_operator(_day)
current_month = "current-month" + _match_with_operator(_month)
current_year = "current-year" + _match_with_operator(_year)
current_day_of_week = "current-day-of-week" + _match_with_operator(_day_of_week)
schedule = ("schedule" + equality_operators + _schedule).setParseAction(
    convert_equality_to_at
)
created_at = "created-at" + range_operators + (interval | iso_datetime)
updated_at = "updated-at" + range_operators + (interval | iso_datetime)
closed_at = "closed-at" + range_operators + (interval | iso_datetime)
merged_at = "merged-at" + range_operators + (interval | iso_datetime)
current_datetime = "current-datetime" + range_operators + iso_datetime

quantifiable_attributes = (
    head
    | base
    | author
    | merged_by
    | body
    | assignee
    | label
    | title
    | files
    | milestone
    | number
    | review_requests
    | review_approved_by
    | review_dismissed_by
    | review_changes_requested_by
    | review_commented_by
    | status_success
    | status_neutral
    | status_failure
    | check_success
    | check_neutral
    | check_success_or_neutral
    | check_failure
    | check_skipped
    | check_pending
    | check_stale
)

locked = _match_boolean("locked")
merged = _match_boolean("merged")
closed = _match_boolean("closed")
conflict = _match_boolean("conflict")
draft = _match_boolean("draft")

non_quantifiable_attributes = locked | closed | conflict | draft | merged

datetime_attributes = (
    current_time
    | current_day_of_week
    | current_month
    | current_year
    | current_day
    | schedule
    | updated_at
    | created_at
    | merged_at
    | closed_at
    | current_datetime
)

search = (
    datetime_attributes
    | (
        pyparsing.Optional(
            (
                pyparsing.Literal("-").setParseAction(pyparsing.replaceWith(True))
                | pyparsing.Literal("¬").setParseAction(pyparsing.replaceWith(True))
                | pyparsing.Literal("+").setParseAction(pyparsing.replaceWith(False))
            ),
            default=False,
        )
        + (
            (pyparsing.Optional("#", default="") + quantifiable_attributes)
            | non_quantifiable_attributes
        )
    )
).setParseAction(_token_to_dict)
