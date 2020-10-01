# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2020 Mergify SAS
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
import pyparsing
import pytest

from mergify_engine.rules import parser


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
        ("locked", {"=": ("locked", True)}),
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
        ("check-success='my quoted ci'", {"=": ("check-success", "my quoted ci")}),
        (
            'check-success="my double quoted ci"',
            {"=": ("check-success", "my double quoted ci")},
        ),
    ),
)
def test_search(line, result):
    assert result == tuple(parser.search.parseString(line, parseAll=True))[0]


@pytest.mark.parametrize(
    "line",
    (
        "arf",
        "-heyo",
        "locked=1",
        "++head=master",
        "foo=bar",
        "#foo=bar",
        "number=foo",
        "author=%foobar",
    ),
)
def test_invalid(line):
    with pytest.raises(pyparsing.ParseException):
        parser.search.parseString(line, parseAll=True)
