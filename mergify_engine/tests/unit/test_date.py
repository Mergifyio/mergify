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
import datetime
import zoneinfo

from freezegun import freeze_time
import pytest

from mergify_engine import date


@pytest.mark.parametrize(
    "value, expected",
    (
        (
            "2021-06-01",
            datetime.datetime(2021, 6, 1, tzinfo=datetime.timezone.utc),
        ),
        (
            "2021-06-01 18:41:39Z",
            datetime.datetime(2021, 6, 1, 18, 41, 39, tzinfo=datetime.timezone.utc),
        ),
        (
            "2021-06-01H18:41:39Z",
            datetime.datetime(2021, 6, 1, 18, 41, 39, tzinfo=datetime.timezone.utc),
        ),
        (
            "2021-06-01T18:41:39",
            datetime.datetime(2021, 6, 1, 18, 41, 39, tzinfo=datetime.timezone.utc),
        ),
        (
            "2021-06-01T18:41:39+00:00",
            datetime.datetime(2021, 6, 1, 18, 41, 39, tzinfo=datetime.timezone.utc),
        ),
        (
            "2022-01-01T01:01:01+00:00",
            datetime.datetime(2022, 1, 1, 1, 1, 1, tzinfo=datetime.timezone.utc),
        ),
        (
            "2022-01-01T01:01:01-02:00",
            datetime.datetime(2022, 1, 1, 3, 1, 1, tzinfo=datetime.timezone.utc),
        ),
        (
            "2022-01-01T01:01:01+02:00",
            datetime.datetime(2021, 12, 31, 23, 1, 1, tzinfo=datetime.timezone.utc),
        ),
    ),
)
def test_fromisoformat(value: str, expected: datetime.datetime) -> None:
    assert date.fromisoformat(value) == expected


@pytest.mark.parametrize(
    "dt,expected_string",
    [
        (
            datetime.datetime(2021, 6, 1, tzinfo=datetime.timezone.utc),
            "2021-06-01 00:00 UTC",
        ),
        (
            datetime.datetime(2021, 12, 31, 23, 1, 1, tzinfo=datetime.timezone.utc),
            "2021-12-31 23:01 UTC",
        ),
        (
            datetime.datetime(
                2021, 12, 31, 23, 1, 0, 999, tzinfo=datetime.timezone.utc
            ),
            "2021-12-31 23:01 UTC",
        ),
        (
            datetime.datetime(
                2021, 12, 31, 23, 0, 0, 999, tzinfo=datetime.timezone.utc
            ),
            "2021-12-31 23:00 UTC",
        ),
    ],
)
def test_pretty_datetime(dt: datetime.datetime, expected_string: str) -> None:
    assert date.pretty_datetime(dt) == expected_string


def test_time_compare():
    with freeze_time("2012-01-14T12:15:00", tz_offset=0):
        utc = datetime.timezone.utc
        assert date.Time(12, 0, utc) < date.utcnow()
        assert date.Time(15, 45, utc) > date.utcnow()
        assert date.Time(12, 15, utc) == date.utcnow()
        assert date.utcnow() > date.Time(12, 0, utc)
        assert date.utcnow() < date.Time(15, 45, utc)
        assert date.utcnow() == date.Time(12, 15, utc)
        assert date.Time(13, 15, utc) == date.Time(13, 15, utc)
        assert date.Time(13, 15, utc) < date.Time(15, 15, utc)
        assert date.Time(15, 0, utc) > date.Time(5, 0, utc)

        zone = zoneinfo.ZoneInfo("Europe/Paris")
        assert date.Time(10, 0, zone) < date.utcnow()
        assert date.Time(18, 45, zone) > date.utcnow()
        assert date.Time(13, 15, zone) == date.utcnow()
        assert date.utcnow() > date.Time(10, 0, zone)
        assert date.utcnow() < date.Time(18, 45, zone)
        assert date.utcnow() == date.Time(13, 15, zone)
        assert date.Time(13, 15, zone) == date.Time(13, 15, zone)
        assert date.Time(13, 15, zone) < date.Time(15, 15, zone)
        assert date.Time(15, 0, zone) > date.Time(5, 0, zone)

        assert date.utcnow() == date.utcnow()
        assert (date.utcnow() > date.utcnow()) is False
