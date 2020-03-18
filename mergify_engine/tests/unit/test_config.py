# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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

from unittest import mock

import pytest
import voluptuous

from mergify_engine import mergify_pull
from mergify_engine import rules


def test_pull_request_rule():
    for valid in (
        {"name": "hello", "conditions": ["head:master"], "actions": {}},
        {"name": "hello", "conditions": ["base:foo", "base:baz"], "actions": {}},
    ):
        rules.load_pull_request_rules_schema([valid])


def test_same_names():
    pull_request_rules = rules.load_pull_request_rules_schema(
        [
            {"name": "hello", "conditions": [], "actions": {}},
            {"name": "foobar", "conditions": [], "actions": {}},
            {"name": "hello", "conditions": [], "actions": {}},
        ]
    )
    assert [rule["name"] for rule in pull_request_rules] == [
        "hello #1",
        "foobar",
        "hello #2",
    ]


def test_user_configuration_schema():
    with pytest.raises(voluptuous.Invalid) as exc_info:
        rules.UserConfigurationSchema("- no\n* way")
    assert exc_info.value.__class__.__name__, "YamlInvalid"
    assert str(exc_info.value.path) == "[at position 2:2]"
    assert exc_info.value.path == [{"line": 2, "column": 2}]

    with pytest.raises(voluptuous.Invalid):
        rules.UserConfigurationSchema(
            """
pull_request_rules:
  - name: ahah
    key: not really what we expected
"""
        )

    with pytest.raises(voluptuous.Invalid):
        rules.UserConfigurationSchema(
            """
pull_request_rules:
"""
        )

    with pytest.raises(voluptuous.Invalid):
        rules.UserConfigurationSchema("")


def test_pull_request_rule_schema_invalid():
    for invalid, match in (
        (
            {"name": "hello", "conditions": ["this is wrong"], "actions": {}},
            "Invalid condition ",
        ),
        (
            {"name": "invalid regexp", "conditions": ["head~=(lol"], "actions": {}},
            r"Invalid condition 'head~=\(lol'. Invalid arguments: "
            r"missing \), "
            r"unterminated subpattern at position 0 @ ",
        ),
        (
            {"name": "hello", "conditions": ["head|4"], "actions": {}},
            "Invalid condition ",
        ),
        (
            {"name": "hello", "conditions": [{"foo": "bar"}], "actions": {}},
            r"expected str @ data\[0\]\['conditions'\]\[0\]",
        ),
        (
            {"name": "hello", "conditions": [], "actions": {}, "foobar": True},
            "extra keys not allowed",
        ),
        (
            {"name": "hello", "conditions": [], "actions": {"merge": True}},
            r"expected a dictionary for dictionary value "
            r"@ data\[0\]\['actions'\]\['merge'\]",
        ),
        (
            {
                "name": "hello",
                "conditions": [],
                "actions": {"backport": {"regexes": ["(azerty"]}},
            },
            r"missing \), unterminated subpattern at position 0 "
            r"@ data\[0\]\['actions'\]\['backport'\]\['regexes'\]\[0\]",
        ),
        (
            {"name": "hello", "conditions": [], "actions": {"backport": True}},
            r"expected a dictionary for dictionary value "
            r"@ data\[0\]\['actions'\]\['backport'\]",
        ),
        (
            {
                "name": "hello",
                "conditions": [],
                "actions": {"merge": {"strict": "yes"}},
            },
            r"expected bool for dictionary value @ "
            r"data\[0\]\['actions'\]\['merge'\]\['strict'\]",
        ),
    ):
        with pytest.raises(voluptuous.MultipleInvalid, match=match):
            print(invalid)
            rules.PullRequestRules([invalid])


@mock.patch("mergify_engine.mergify_pull.MergifyPull.g", return_value=mock.PropertyMock)
@mock.patch(
    "mergify_engine.mergify_pull.MergifyPull.g_pull", return_value=mock.PropertyMock
)
@mock.patch(
    "mergify_engine.mergify_pull.MergifyPull.reviews", new_callable=mock.PropertyMock
)
def test_get_pull_request_rule(reviews, g_pull, g):
    team = mock.Mock()
    team.slug = "my-reviewers"
    team.get_members.return_value = [mock.Mock(login="sileht"), mock.Mock(login="jd")]

    org = mock.Mock()
    org.get_teams.return_value = [team]
    g.get_organization.return_value = org

    file1 = mock.Mock()
    file1.filename = "README.rst"
    file2 = mock.Mock()
    file2.filename = "setup.py"
    g_pull.get_files.return_value = [file1, file2]

    review = {
        "user": {"login": "sileht", "type": "User"},
        "state": "APPROVED",
        "author_association": "MEMBER",
    }
    reviews.return_value = [review]
    client = mock.Mock()
    client.item.return_value = {"permission": "write"}

    pull_request = mergify_pull.MergifyPull(
        client,
        {
            "number": 1,
            "html_url": "<html_url>",
            "state": "closed",
            "merged_by": None,
            "merged_at": None,
            "merged": False,
            "milestone": None,
            "mergeable_state": "unstable",
            "assignees": [],
            "labels": [],
            "author": "jd",
            "base": {"ref": "master", "repo": {"name": "name", "private": False}},
            "head": {"ref": "myfeature"},
            "locked": False,
            "requested_reviewers": [],
            "requested_teams": [],
            "title": "My awesome job",
            "body": "I rock",
            "user": {"login": "another-jd"},
        },
    )

    # Don't catch data in these tests
    pull_request.to_dict = pull_request._get_consolidated_data

    fake_ci = mock.Mock()
    fake_ci.context = "continuous-integration/fake-ci"
    fake_ci.state = "success"
    pull_request._get_checks = mock.Mock()
    pull_request._get_checks.return_value = [fake_ci]

    # Empty conditions
    pull_request_rules = rules.PullRequestRules(
        [{"name": "default", "conditions": [], "actions": {}}]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule["actions"] == {}

    pull_request_rules = rules.PullRequestRules(
        [{"name": "hello", "conditions": ["base:master"], "actions": {}}]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["hello"]
    assert [r["name"] for r, _ in match.matching_rules] == ["hello"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule["actions"] == {}

    pull_request_rules = rules.PullRequestRules(
        [
            {"name": "hello", "conditions": ["base:master"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["hello", "backport"]
    assert [r["name"] for r, _ in match.matching_rules] == ["hello", "backport"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule["actions"] == {}

    pull_request_rules = rules.PullRequestRules(
        [
            {"name": "hello", "conditions": ["#files=3"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["hello", "backport"]
    assert [r["name"] for r, _ in match.matching_rules] == ["backport"]
    for rule in match.rules:
        assert rule["actions"] == {}

    pull_request_rules = rules.PullRequestRules(
        [
            {"name": "hello", "conditions": ["#files=2"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["hello", "backport"]
    assert [r["name"] for r, _ in match.matching_rules] == ["hello", "backport"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule["actions"] == {}

    # No match
    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=xyz",
                    "status-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["merge"]
    assert [r["name"] for r, _ in match.matching_rules] == []

    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=master",
                    "status-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["merge"]
    assert [r["name"] for r, _ in match.matching_rules] == ["merge"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule["actions"] == {}

    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=master",
                    "status-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            },
            {
                "name": "fast merge",
                "conditions": [
                    "base=master",
                    "label=fast-track",
                    "status-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge with alternate ci",
                "conditions": [
                    "base=master",
                    "label=fast-track",
                    "status-success=continuous-integration/fake-ci-bis",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge from a bot",
                "conditions": [
                    "base=master",
                    "author=mybot",
                    "status-success=continuous-integration/fake-ci",
                ],
                "actions": {},
            },
        ]
    )
    match = pull_request_rules.get_pull_request_rule(pull_request)

    assert [r["name"] for r in match.rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
        "fast merge from a bot",
    ]
    assert [r["name"] for r, _ in match.matching_rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
    ]
    for rule in match.rules:
        assert rule["actions"] == {}

    assert match.matching_rules[0][0]["name"] == "merge"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == "#approved-reviews-by>=2"

    assert match.matching_rules[1][0]["name"] == "fast merge"
    assert len(match.matching_rules[1][1]) == 1
    assert str(match.matching_rules[1][1][0]) == "label=fast-track"

    assert match.matching_rules[2][0]["name"] == "fast merge with alternate ci"
    assert len(match.matching_rules[2][1]) == 2
    assert str(match.matching_rules[2][1][0]) == "label=fast-track"
    assert (
        str(match.matching_rules[2][1][1])
        == "status-success=continuous-integration/fake-ci-bis"
    )

    # Team conditions with one review missing
    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "default",
                "conditions": [
                    "approved-reviews-by=@orgs/my-reviewers",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]

    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == "#approved-reviews-by>=2"

    review2 = {
        "user": {"login": "jd", "type": "User"},
        "state": "APPROVED",
        "author_association": "MEMBER",
    }
    reviews.return_value = [review, review2]

    # Team conditions with no review missing
    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "default",
                "conditions": [
                    "approved-reviews-by=@orgs/my-reviewers",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]

    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Forbidden labels, when no label set
    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "default",
                "conditions": ["-label~=^(status/wip|status/blocked|review/need2)$"],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Forbidden labels, when forbiden label set
    pull_request.data["labels"] = [{"name": "status/wip"}]

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == (
        "-label~=^(status/wip|status/blocked|review/need2)$"
    )

    # Forbidden labels, when other label set
    pull_request.data["labels"] = [{"name": "allowed"}]

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Test team expander
    pull_request_rules = rules.PullRequestRules(
        [
            {
                "name": "default",
                "conditions": ["author~=^(user1|user2|another-jd)$"],
                "actions": {},
            }
        ]
    )
    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r["name"] for r in match.rules] == ["default"]
    assert [r["name"] for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0]["name"] == "default"
    assert len(match.matching_rules[0][1]) == 0
