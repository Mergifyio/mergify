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
import zoneinfo

from freezegun import freeze_time
import pyparsing
import pytest

from mergify_engine import date
from mergify_engine.rules import parser


now = datetime.datetime.fromisoformat("2012-01-14T20:32:00+00:00")


@pytest.mark.parametrize(
    "line, result",
    (
        (
            "schedule!=Mon-Fri 12:00-23:59[Europe/Paris]",
            {
                "-": {
                    "@": (
                        "schedule",
                        {
                            "and": (
                                {
                                    "and": (
                                        {
                                            ">=": (
                                                "current-day-of-week",
                                                date.DayOfWeek(1),
                                            )
                                        },
                                        {
                                            "<=": (
                                                "current-day-of-week",
                                                date.DayOfWeek(5),
                                            )
                                        },
                                    )
                                },
                                {
                                    "and": (
                                        {
                                            ">=": (
                                                "current-time",
                                                date.Time(
                                                    12,
                                                    0,
                                                    tzinfo=zoneinfo.ZoneInfo(
                                                        "Europe/Paris"
                                                    ),
                                                ),
                                            )
                                        },
                                        {
                                            "<=": (
                                                "current-time",
                                                date.Time(
                                                    23,
                                                    59,
                                                    tzinfo=zoneinfo.ZoneInfo(
                                                        "Europe/Paris"
                                                    ),
                                                ),
                                            )
                                        },
                                    )
                                },
                            )
                        },
                    )
                }
            },
        ),
        (
            "schedule!=Mon-Fri 12:00-23:59",
            {
                "-": {
                    "@": (
                        "schedule",
                        {
                            "and": (
                                {
                                    "and": (
                                        {
                                            ">=": (
                                                "current-day-of-week",
                                                date.DayOfWeek(1),
                                            )
                                        },
                                        {
                                            "<=": (
                                                "current-day-of-week",
                                                date.DayOfWeek(5),
                                            )
                                        },
                                    )
                                },
                                {
                                    "and": (
                                        {
                                            ">=": (
                                                "current-time",
                                                date.Time(
                                                    12, 0, tzinfo=datetime.timezone.utc
                                                ),
                                            )
                                        },
                                        {
                                            "<=": (
                                                "current-time",
                                                date.Time(
                                                    23, 59, tzinfo=datetime.timezone.utc
                                                ),
                                            )
                                        },
                                    )
                                },
                            )
                        },
                    )
                }
            },
        ),
        ("base:main", {"=": ("base", "main")}),
        ("base!=main", {"!=": ("base", "main")}),
        ("base~=^stable/", {"~=": ("base", "^stable/")}),
        ("-base:foobar", {"-": {"=": ("base", "foobar")}}),
        ("-author~=jd", {"-": {"~=": ("author", "jd")}}),
        ("¬author~=jd", {"-": {"~=": ("author", "jd")}}),
        ("conflict", {"=": ("conflict", True)}),
        (
            "current-time>=10:00[PST8PDT]",
            {
                ">=": (
                    "current-time",
                    date.Time(10, 0, tzinfo=zoneinfo.ZoneInfo("PST8PDT")),
                )
            },
        ),
        (
            "current-time>=10:00",
            {
                ">=": (
                    "current-time",
                    date.Time(10, 0, tzinfo=datetime.timezone.utc),
                )
            },
        ),
        ("current-day=4", {"=": ("current-day", date.Day(4))}),
        ("current-month=5", {"=": ("current-month", date.Month(5))}),
        ("current-year=2000", {"=": ("current-year", date.Year(2000))}),
        ("current-day-of-week=4", {"=": ("current-day-of-week", date.DayOfWeek(4))}),
        ("current-day-of-week=MON", {"=": ("current-day-of-week", date.DayOfWeek(1))}),
        (
            "current-day-of-week=WednesDay",
            {"=": ("current-day-of-week", date.DayOfWeek(3))},
        ),
        ("current-day-of-week=sun", {"=": ("current-day-of-week", date.DayOfWeek(7))}),
        (
            "schedule=MON-friday 10:02-22:35",
            {
                "@": (
                    "schedule",
                    {
                        "and": (
                            {
                                "and": (
                                    {">=": ("current-day-of-week", date.DayOfWeek(1))},
                                    {"<=": ("current-day-of-week", date.DayOfWeek(5))},
                                )
                            },
                            {
                                "and": (
                                    {
                                        ">=": (
                                            "current-time",
                                            date.Time(
                                                10, 2, tzinfo=datetime.timezone.utc
                                            ),
                                        )
                                    },
                                    {
                                        "<=": (
                                            "current-time",
                                            date.Time(
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
            "schedule=MON-friday",
            {
                "@": (
                    "schedule",
                    {
                        "and": (
                            {">=": ("current-day-of-week", date.DayOfWeek(1))},
                            {"<=": ("current-day-of-week", date.DayOfWeek(5))},
                        )
                    },
                )
            },
        ),
        (
            "schedule=10:02-22:35",
            {
                "@": (
                    "schedule",
                    {
                        "and": (
                            {
                                ">=": (
                                    "current-time",
                                    date.Time(10, 2, tzinfo=datetime.timezone.utc),
                                )
                            },
                            {
                                "<=": (
                                    "current-time",
                                    date.Time(22, 35, tzinfo=datetime.timezone.utc),
                                )
                            },
                        )
                    },
                )
            },
        ),
        ("locked", {"=": ("locked", True)}),
        ("up-to-date", {"=": ("up-to-date", True)}),
        (
            f"merged-at<={now.isoformat()}",
            {"<=": ("merged-at", now)},
        ),
        (
            f"closed-at<={now.isoformat()}",
            {"<=": ("closed-at", now)},
        ),
        (
            f"merged-at<={now.isoformat()}",
            {"<=": ("merged-at", now)},
        ),
        (
            f"updated-at<={now.isoformat()}",
            {"<=": ("updated-at", now)},
        ),
        (
            "closed-at>=18:02 ago",
            {
                ">=": (
                    "closed-at-relative",
                    date.RelativeDatetime(
                        now - datetime.timedelta(hours=18, minutes=2)
                    ),
                )
            },
        ),
        (
            "merged-at>18:02 ago",
            {
                ">": (
                    "merged-at-relative",
                    date.RelativeDatetime(
                        now - datetime.timedelta(hours=18, minutes=2)
                    ),
                )
            },
        ),
        (
            "created-at<18:02 ago",
            {
                "<": (
                    "created-at-relative",
                    date.RelativeDatetime(
                        now - datetime.timedelta(hours=18, minutes=2)
                    ),
                ),
            },
        ),
        (
            "updated-at>=18:02 ago",
            {
                ">=": (
                    "updated-at-relative",
                    date.RelativeDatetime(
                        now - datetime.timedelta(hours=18, minutes=2)
                    ),
                )
            },
        ),
        (
            "updated-at<=7 days ago",
            {
                "<=": (
                    "updated-at-relative",
                    date.RelativeDatetime(now - datetime.timedelta(days=7)),
                )
            },
        ),
        (
            "updated-at>7 days 18:02 ago",
            {
                ">": (
                    "updated-at-relative",
                    date.RelativeDatetime(
                        now - datetime.timedelta(days=7, hours=18, minutes=2),
                    ),
                )
            },
        ),
        (
            f"current-timestamp<={now.isoformat()}",
            {"<=": ("current-timestamp", now)},
        ),
        ("-linear-history", {"-": {"=": ("linear-history", True)}}),
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
        "++head=main",
        "foo=bar",
        "#foo=bar",
        "number=foo",
        "author=%foobar",
        "current-time<foobar",
        "current-time=10:00",
        "-current-time>=10:00",
        "current-day-of-week=100",
        "current-month=100",
        "current-year=0",
        "current-day=100",
        "current-day>100",
        "updated-at=7 days 18:00",
        "updated-at>=100",
        "current-timestamp>=100",
        "current-time>=10:00[InvalidTZ]",
        "schedule=MON-friday 10:02-22:35[InvalidTZ]",
    ),
)
def test_invalid(line):
    with pytest.raises(pyparsing.ParseException):
        parser.search.parseString(line, parseAll=True)
