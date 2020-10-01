# -*- encoding: utf-8 -*-
#
# Copyright © 2018–2020 Mergify SAS
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
from base64 import encodebytes
from unittest import mock

import pytest
import voluptuous

from mergify_engine import context
from mergify_engine import rules
from mergify_engine.rules import InvalidRules
from mergify_engine.rules import get_mergify_config


def pull_request_rule_from_list(lst):
    return voluptuous.Schema(rules.PullRequestRulesSchema)(lst)


def test_valid_condition():
    c = rules.PullRequestRuleCondition("head~=bar")
    assert str(c) == "head~=bar"


def test_invalid_condition_re():
    with pytest.raises(voluptuous.Invalid):
        rules.PullRequestRuleCondition("head~=(bar")


@pytest.mark.parametrize(
    "valid",
    (
        {"name": "hello", "conditions": ["head:master"], "actions": {}},
        {"name": "hello", "conditions": ["base:foo", "base:baz"], "actions": {}},
    ),
)
def test_pull_request_rule(valid):
    pull_request_rule_from_list([valid])


def test_same_names():
    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": [], "actions": {}},
            {"name": "foobar", "conditions": [], "actions": {}},
            {"name": "hello", "conditions": [], "actions": {}},
        ]
    )
    assert [rule.name for rule in pull_request_rules] == [
        "hello #1",
        "foobar",
        "hello #2",
    ]


def test_jinja_with_list_attribute():
    pull_request_rules = rules.UserConfigurationSchema(
        """
        pull_request_rules:
          - name: ahah
            conditions:
            - base=master
            actions:
              comment:
                message: |
                  This pull request has been approved by:
                  {% for name in approved_reviews_by %}
                  @{{name}}
                  {% endfor %}
                  Thank you @{{author}} for your contributions!
        """
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]


def test_jinja_with_wrong_syntax():
    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=master
                actions:
                  comment:
                    message: |
                      This pull request has been approved by:
                      {% for name in approved_reviews_by %}
                      Thank you @{{author}} for your contributions!
            """
        )
    assert str(i.value) == (
        "Template syntax error @ data['pull_request_rules']"
        "[0]['actions']['comment']['message'][line 3]"
    )

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=master
                actions:
                  comment:
                    message: |
                      This pull request has been approved by:
                      {% for name in approved_reviews_by %}
                      @{{ name }}
                      {% endfor %}
                      Thank you @{{foo}} for your contributions!
            """
        )
    assert str(i.value) == (
        "Template syntax error for dictionary value @ data['pull_request_rules']"
        "[0]['actions']['comment']['message']"
    )


@pytest.mark.parametrize(
    "valid",
    (
        (
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=master
                actions:
                  comment:
                    message: |
                      This pull request has been approved by
            """
        ),
        (
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=master
                actions:
                  comment:
                    message: |
                      This pull request has been approved by
                      {% for name in approved_reviews_by %}
                      @{{ name }}
                      {% endfor %}
            """
        ),
    ),
)
def test_get_mergify_config(valid):
    client = mock.Mock()
    client.item.return_value = {"content": encodebytes(valid.encode()).decode()}
    filename, schema = get_mergify_config(client, "xyz")
    assert isinstance(schema, dict)
    assert "pull_request_rules" in schema


@pytest.mark.parametrize(
    "invalid",
    (
        (
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=master
                actions:
                  comment:
            """
        ),
        (
            """
            pull_request_rules:
              - name: ahah
                conditions:
                actions:
                  coment:
                    message: |
                      This pull request has been approved by
                      {% for name in approved_reviews_by %}
                      @{{ name }}
            """
        ),
    ),
)
def test_get_mergify_config_invalid(invalid):
    with pytest.raises(InvalidRules):
        client = mock.Mock()
        client.item.return_value = {"content": encodebytes(invalid.encode()).decode()}
        filename, schema = get_mergify_config(client, "xyz")


def test_user_configuration_schema():
    with pytest.raises(voluptuous.Invalid) as exc_info:
        rules.UserConfigurationSchema("- no\n* way")
    assert str(exc_info.value) == "Invalid YAML at [line 2, column 2]"

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            """
            pull_request_rules:
              - name: ahah
                key: not really what we expected
            """
        )
    assert (
        str(i.value) == "extra keys not allowed @ data['pull_request_rules'][0]['key']"
    )

    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert str(ir) == (
        "* extra keys not allowed @ data['pull_request_rules'][0]['key']\n"
        "* required key not provided @ data['pull_request_rules'][0]['actions']\n"
        "* required key not provided @ data['pull_request_rules'][0]['conditions']"
    )
    assert [] == ir.get_annotations(".mergify.yml")

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            """invalid:
- *yaml
"""
        )
    assert str(i.value) == "Invalid YAML at [line 2, column 3]"

    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert (
        str(ir)
        == """Invalid YAML at [line 2, column 3]
```
found undefined alias 'yaml'
  in "<unicode string>", line 2, column 3:
    - *yaml
      ^
```"""
    )
    assert [
        {
            "annotation_level": "failure",
            "end_column": 3,
            "end_line": 2,
            "message": "found undefined alias 'yaml'\n"
            '  in "<unicode string>", line 2, column 3:\n'
            "    - *yaml\n"
            "      ^",
            "path": ".mergify.yml",
            "start_column": 3,
            "start_line": 2,
            "title": "Invalid YAML",
        }
    ] == ir.get_annotations(".mergify.yml")

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            """
pull_request_rules:
"""
        )
    assert (
        str(i.value)
        == "expected a list for dictionary value @ data['pull_request_rules']"
    )

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema("")
    assert str(i.value) == "expected a dictionary"


def test_user_binary_file():
    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(chr(4))
    assert str(i.value) == "Invalid YAML at []"
    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert (
        str(ir)
        == """Invalid YAML at []
```
unacceptable character #x0004: special characters are not allowed
  in "<unicode string>", position 0
```"""
    )
    assert ir.get_annotations(".mergify.yml") == []


@pytest.mark.parametrize(
    "invalid,match",
    (
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
        (
            {
                "name": "hello",
                "conditions": [],
                "actions": {"review": {"message": "{{syntax error"}},
            },
            r"Template syntax error @ data\[0\]\['actions'\]\['review'\]\['message'\]\[line 1\]",
        ),
        (
            {
                "name": "hello",
                "conditions": [],
                "actions": {"review": {"message": "{{unknownattribute}}"}},
            },
            r"Template syntax error for dictionary value @ data\[0\]\['actions'\]\['review'\]\['message'\]",
        ),
    ),
)
def test_pull_request_rule_schema_invalid(invalid, match):
    with pytest.raises(voluptuous.MultipleInvalid, match=match):
        pull_request_rule_from_list([invalid])


def test_get_pull_request_rule():

    client = mock.Mock()

    get_reviews = [
        {
            "user": {"login": "sileht", "type": "User"},
            "state": "APPROVED",
            "author_association": "MEMBER",
        }
    ]
    get_files = [{"filename": "README.rst"}, {"filename": "setup.py"}]
    get_team_members = [{"login": "sileht"}, {"login": "jd"}]

    get_checks = []
    get_statuses = [{"context": "continuous-integration/fake-ci", "state": "success"}]
    client.item.return_value = {"permission": "write"}  # get review user perm

    def client_items(url, *args, **kwargs):
        if url == "/repos/another-jd/name/pulls/1/reviews":
            return get_reviews
        elif url == "/repos/another-jd/name/pulls/1/files":
            return get_files
        elif url == "/repos/another-jd/name/commits/<sha>/check-runs":
            return get_checks
        elif url == "/repos/another-jd/name/commits/<sha>/status":
            return get_statuses
        elif url == "/orgs/orgs/teams/my-reviewers/members":
            return get_team_members
        else:
            raise RuntimeError(f"not handled url {url}")

    client.items.side_effect = client_items

    ctxt = context.Context(
        client,
        {
            "number": 1,
            "html_url": "<html_url>",
            "state": "closed",
            "merged_by": None,
            "merged_at": None,
            "merged": False,
            "draft": False,
            "milestone": None,
            "mergeable_state": "unstable",
            "assignees": [],
            "labels": [],
            "base": {
                "ref": "master",
                "repo": {"name": "name", "private": False},
                "user": {"login": "another-jd"},
                "sha": "mew",
            },
            "head": {"ref": "myfeature", "sha": "<sha>"},
            "locked": False,
            "requested_reviewers": [],
            "requested_teams": [],
            "title": "My awesome job",
            "body": "I rock",
            "user": {"login": "another-jd"},
        },
        {},
    )

    # Empty conditions
    pull_request_rules = rules.PullRequestRules(
        [rules.Rule(name="default", conditions=[], actions={})]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [{"name": "hello", "conditions": ["base:master"], "actions": {}}]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello"]
    assert [r.name for r, _ in match.matching_rules] == ["hello"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["base:master"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r, _ in match.matching_rules] == ["hello", "backport"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["author:foobar"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r, _ in match.matching_rules] == ["backport"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["author:another-jd"], "actions": {}},
            {"name": "backport", "conditions": ["base:master"], "actions": {}},
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r, _ in match.matching_rules] == ["hello", "backport"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule.actions == {}

    # No match
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=xyz",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["merge"]
    assert [r.name for r, _ in match.matching_rules] == []

    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=master",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["merge"]
    assert [r.name for r, _ in match.matching_rules] == ["merge"]
    assert [(r, []) for r in match.rules] == match.matching_rules
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=master",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            },
            {
                "name": "fast merge",
                "conditions": [
                    "base=master",
                    "label=fast-track",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge with alternate ci",
                "conditions": [
                    "base=master",
                    "label=fast-track",
                    "check-success=continuous-integration/fake-ci-bis",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge from a bot",
                "conditions": [
                    "base=master",
                    "author=mybot",
                    "check-success=continuous-integration/fake-ci",
                ],
                "actions": {},
            },
        ]
    )
    match = pull_request_rules.get_pull_request_rule(ctxt)

    assert [r.name for r in match.rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
        "fast merge from a bot",
    ]
    assert [r.name for r, _ in match.matching_rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
    ]
    for rule in match.rules:
        assert rule.actions == {}

    assert match.matching_rules[0][0].name == "merge"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == "#approved-reviews-by>=2"

    assert match.matching_rules[1][0].name == "fast merge"
    assert len(match.matching_rules[1][1]) == 1
    assert str(match.matching_rules[1][1][0]) == "label=fast-track"

    assert match.matching_rules[2][0].name == "fast merge with alternate ci"
    assert len(match.matching_rules[2][1]) == 2
    assert str(match.matching_rules[2][1][0]) == "label=fast-track"
    assert (
        str(match.matching_rules[2][1][1])
        == "check-success=continuous-integration/fake-ci-bis"
    )

    # Team conditions with one review missing
    pull_request_rules = pull_request_rule_from_list(
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

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]

    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == "#approved-reviews-by>=2"

    get_reviews.append(
        {
            "user": {"login": "jd", "type": "User"},
            "state": "APPROVED",
            "author_association": "MEMBER",
        }
    )

    del ctxt.__dict__["reviews"]
    del ctxt.__dict__["consolidated_reviews"]

    # Team conditions with no review missing
    pull_request_rules = pull_request_rule_from_list(
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

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]

    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Forbidden labels, when no label set
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "default",
                "conditions": ["-label~=^(status/wip|status/blocked|review/need2)$"],
                "actions": {},
            }
        ]
    )

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Forbidden labels, when forbiden label set
    ctxt.pull["labels"] = [{"name": "status/wip"}]

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 1
    assert str(match.matching_rules[0][1][0]) == (
        "-label~=^(status/wip|status/blocked|review/need2)$"
    )

    # Forbidden labels, when other label set
    ctxt.pull["labels"] = [{"name": "allowed"}]

    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 0

    # Test team expander
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "default",
                "conditions": ["author~=^(user1|user2|another-jd)$"],
                "actions": {},
            }
        ]
    )
    match = pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r, _ in match.matching_rules] == ["default"]
    assert match.matching_rules[0][0].name == "default"
    assert len(match.matching_rules[0][1]) == 0


def test_check_runs_custom():
    pull_request_rules = rules.UserConfigurationSchema(
        """
pull_request_rules:
  - name: ahah
    conditions:
    - base=master
    actions:
      post_check:
        title: '{{ check_rule_name }} whatever'
        summary: |
          This pull request has been checked!
          Thank you @{{author}} for your contributions!

          {{ check_conditions }}

"""
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]


def test_check_runs_default():
    pull_request_rules = rules.UserConfigurationSchema(
        """
pull_request_rules:
  - name: ahah
    conditions:
    - base=master
    actions:
      post_check: {}
"""
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]
