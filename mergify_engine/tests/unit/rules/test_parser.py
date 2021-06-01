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

from freezegun import freeze_time
import pyparsing
import pytest

from mergify_engine import date
from mergify_engine.rules import parser


now = datetime.datetime.fromisoformat("2012-01-14T20:32:00+00:00")


@pytest.mark.parametrize(
    "line, result",
    (
        ("base:master", {"=": ("base", "master")}),
        ("base!=master", {"!=": ("base", "master")}),
        ("base~=^stable/", {"~=": ("base", "^stable/")}),
        ("-base:foobar", {"-": {"=": ("base", "foobar")}}),
        ("-author~=jd", {"-": {"~=": ("author", "jd")}}),
        ("¬author~=jd", {"-": {"~=": ("author", "jd")}}),
        ("conflict", {"=": ("conflict", True)}),
        (
            "time>=10:00",
            {">=": ("time", datetime.time(10, 0, tzinfo=datetime.timezone.utc))},
        ),
        ("day=4", {"=": ("day", date.Day(4))}),
        ("month=5", {"=": ("month", date.Month(5))}),
        ("year=2000", {"=": ("year", date.Year(2000))}),
        ("day-of-week=4", {"=": ("day-of-week", date.DayOfWeek(4))}),
        ("day-of-week=MON", {"=": ("day-of-week", date.DayOfWeek(1))}),
        ("day-of-week=WednesDay", {"=": ("day-of-week", date.DayOfWeek(3))}),
        ("day-of-week=sun", {"=": ("day-of-week", date.DayOfWeek(7))}),
        (
            "calendar=MON-friday 10:02-22:35",
            {
                "@": (
                    "calendar",
                    {
                        "and": (
                            {
                                "and": (
                                    {">=": ("day-of-week", date.DayOfWeek(1))},
                                    {"<=": ("day-of-week", date.DayOfWeek(5))},
                                )
                            },
                            {
                                "and": (
                                    {
                                        ">=": (
                                            "time",
                                            datetime.time(
                                                10, 2, tzinfo=datetime.timezone.utc
                                            ),
                                        )
                                    },
                                    {
                                        "<=": (
                                            "time",
                                            datetime.time(
                                                22, 35, tzinfo=datetime.timezone.utc
                                            ),
                                        )
                                    },
                                )
                            },
                        )
                    },
                )
            },
        ),
        (
            "calendar=MON-friday",
            {
                "@": (
                    "calendar",
                    {
                        "and": (
                            {">=": ("day-of-week", date.DayOfWeek(1))},
                            {"<=": ("day-of-week", date.DayOfWeek(5))},
                        )
                    },
                )
            },
        ),
        (
            "calendar=10:02-22:35",
            {
                "@": (
                    "calendar",
                    {
                        "and": (
                            {
                                ">=": (
                                    "time",
                                    datetime.time(10, 2, tzinfo=datetime.timezone.utc),
                                )
                            },
                            {
                                "<=": (
                                    "time",
                                    datetime.time(22, 35, tzinfo=datetime.timezone.utc),
                                )
                            },
                        )
                    },
                )
            },
        ),
        ("locked", {"=": ("locked", True)}),
        (
            "updated-at>=18:02",
            {">=": ("updated-at", now + datetime.timedelta(hours=18, minutes=2))},
        ),
        (
            f"updated-at<={now.isoformat()}",
            {"<=": ("updated-at", now)},
        ),
        (
            "updated-at<=7 days",
            {"<=": ("updated-at", now + datetime.timedelta(days=7))},
        ),
        (
            "updated-at>7 days 18:02",
            {
                ">": (
                    "updated-at",
                    now + datetime.timedelta(days=7, hours=18, minutes=2),
                )
            },
        ),
        ("-locked", {"-": {"=": ("locked", True)}}),
        ("assignee:sileht", {"=": ("assignee", "sileht")}),
        ("#assignee=3", {"=": ("#assignee", 3)}),
        ("#assignee>1", {">": ("#assignee", 1)}),
        ("author=jd", {"=": ("author", "jd")}),
        ("author=mergify[bot]", {"=": ("author", "mergify[bot]")}),
        ("author=foo-bar", {"=": ("author", "foo-bar")}),
        ("#assignee>=2", {">=": ("#assignee", 2)}),
        ("number>=2", {">=": ("number", 2)}),
        ("assignee=@org/team", {"=": ("assignee", "@org/team")}),
        (
            "status-success=my ci has spaces",
            {"=": ("status-success", "my ci has spaces")},
        ),
        ("status-success='my quoted ci'", {"=": ("status-success", "my quoted ci")}),
        (
            'status-success="my double quoted ci"',
            {"=": ("status-success", "my double quoted ci")},
        ),
        (
            "check-success=my ci has spaces",
            {"=": ("check-success", "my ci has spaces")},
        ),
        ("check-success='my quoted ci'", {"=": ("check-success", "my quoted ci")}),
        (
            'check-success="my double quoted ci"',
            {"=": ("check-success", "my double quoted ci")},
        ),
        ("check-failure='my quoted ci'", {"=": ("check-failure", "my quoted ci")}),
        (
            'check-failure="my double quoted ci"',
            {"=": ("check-failure", "my double quoted ci")},
        ),
        ("check-neutral='my quoted ci'", {"=": ("check-neutral", "my quoted ci")}),
        (
            'check-neutral="my double quoted ci"',
            {"=": ("check-neutral", "my double quoted ci")},
        ),
        ("check-skipped='my quoted ci'", {"=": ("check-skipped", "my quoted ci")}),
        (
            'check-skipped="my double quoted ci"',
            {"=": ("check-skipped", "my double quoted ci")},
        ),
        ("check-pending='my quoted ci'", {"=": ("check-pending", "my quoted ci")}),
        (
            'check-pending="my double quoted ci"',
            {"=": ("check-pending", "my double quoted ci")},
        ),
        ("check-stale='my quoted ci'", {"=": ("check-stale", "my quoted ci")}),
        (
            'check-stale="my double quoted ci"',
            {"=": ("check-stale", "my double quoted ci")},
        ),
    ),
)
@freeze_time(now)
def test_search(line, result):
    assert result == tuple(parser.search.parseString(line, parseAll=True))[0]


@pytest.mark.parametrize(
    "line",
    (
        "arf",
        "-heyo",
        "locked=1",
        "#conflict",
        "++head=master",
        "foo=bar",
        "#foo=bar",
        "number=foo",
        "author=%foobar",
        "time>=foo",
        "time>=foo:foo",
        "day-of-week=100",
        "month=100",
        "year=0",
        "day=100",
        "day>100",
        "update-at=7 days 18:00",
        "update-at>=100",
    ),
)
def test_invalid(line):
    with pytest.raises(pyparsing.ParseException):
        parser.search.parseString(line, parseAll=True)
