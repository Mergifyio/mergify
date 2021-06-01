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
