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
import dataclasses
import typing
from unittest import mock

from freezegun import freeze_time
import pytest
import voluptuous

from mergify_engine import context
from mergify_engine import date
from mergify_engine import github_types
from mergify_engine import rules
from mergify_engine import utils
from mergify_engine.clients import http
from mergify_engine.dashboard import subscription
from mergify_engine.rules import InvalidRules
from mergify_engine.rules import conditions
from mergify_engine.rules import get_mergify_config


def pull_request_rule_from_list(lst: typing.Any) -> rules.PullRequestRules:
    return typing.cast(
        rules.PullRequestRules,
        voluptuous.Schema(rules.get_pull_request_rules_schema())(lst),
    )


def test_valid_condition():
    c = conditions.RuleCondition("head~=bar")
    assert str(c) == "head~=bar"


def fake_expander(v: str) -> typing.List[str]:
    return ["foo", "bar"]


@pytest.mark.asyncio
async def test_expanders():
    rc = conditions.RuleCondition("author=@team")
    rc.partial_filter.value_expanders["author"] = fake_expander
    await rc(mock.Mock(author="foo"))
    assert rc.match

    copy_rc = rc.copy()
    await copy_rc(mock.Mock(author="foo"))
    assert copy_rc.match


def test_invalid_condition_re():
    with pytest.raises(voluptuous.Invalid):
        conditions.RuleCondition("head~=(bar")


@dataclasses.dataclass
class FakeQueuePullRequest:
    attrs: typing.Dict[str, context.ContextAttributeType]

    async def __getattr__(self, name: str) -> context.ContextAttributeType:
        fancy_name = name.replace("_", "-")
        return self.attrs[fancy_name]


@pytest.mark.asyncio
async def test_multiple_pulls_to_match():
    c = conditions.QueueRuleConditions(
        [
            conditions.RuleConditionGroup(
                {
                    "or": [
                        conditions.RuleCondition("base=main"),
                        conditions.RuleCondition("base=main"),
                    ]
                }
            )
        ]
    )
    assert await c([FakeQueuePullRequest({"number": 1, "base": "main"})])
    c = c.copy()
    assert await c([FakeQueuePullRequest({"number": 1, "base": "main"})])
    c = c.copy()
    assert not await c([FakeQueuePullRequest({"number": 1, "base": "other"})])
    c = c.copy()
    assert await c(
        [
            FakeQueuePullRequest({"number": 1, "base": "main"}),
            FakeQueuePullRequest({"number": 1, "base": "main"}),
        ]
    )
    c = c.copy()
    assert await c(
        [
            FakeQueuePullRequest({"number": 1, "base": "main"}),
            FakeQueuePullRequest({"number": 1, "base": "main"}),
            FakeQueuePullRequest({"number": 1, "base": "main"}),
        ]
    )
    c = c.copy()
    assert not await c(
        [
            FakeQueuePullRequest({"number": 1, "base": "main"}),
            FakeQueuePullRequest({"number": 1, "base": "main"}),
            FakeQueuePullRequest({"number": 1, "base": "other"}),
        ]
    )


@pytest.mark.parametrize(
    "valid",
    (
        {"name": "hello", "conditions": ["head:main"], "actions": {}},
        {"name": "hello", "conditions": ["body:foo", "body:baz"], "actions": {}},
        {
            "name": "and",
            "conditions": [{"and": ["body:foo", "body:baz"]}],
            "actions": {},
        },
        {"name": "or", "conditions": [{"or": ["body:foo", "body:baz"]}], "actions": {}},
        {
            "name": "and,or",
            "conditions": [{"and": ["label=foo", {"or": ["body:foo", "body:baz"]}]}],
            "actions": {},
        },
        {
            "name": "or,and",
            "conditions": [{"or": ["label=foo", {"and": ["body:foo", "body:baz"]}]}],
            "actions": {},
        },
    ),
)
def test_pull_request_rule(valid):
    pull_request_rule_from_list([valid])


@pytest.mark.parametrize(
    "invalid,error",
    (
        (
            {
                "name": "unknown operator",
                "conditions": [{"what": ["base:foo", "base:baz"]}],
                "actions": {},
            },
            "extra keys not allowed @ data[0]['conditions'][0]['what']",
        ),
        (
            {
                "name": "too many nested conditions",
                "conditions": [
                    {
                        "or": [
                            "label=foo",
                            {
                                "and": [
                                    {
                                        "or": [
                                            "base:foo",
                                            {
                                                "and": [
                                                    "base:baz",
                                                    "base=main",
                                                    {
                                                        "or": [
                                                            "author:robot",
                                                            "author=bot",
                                                        ]
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                    "label=bar",
                                ]
                            },
                        ]
                    }
                ],
                "actions": {},
            },
            "Maximun number of nested conditions reached",
        ),
        (
            {
                "name": "not enought items or",
                "conditions": [{"or": ["label=foo"]}],
                "actions": {},
            },
            "length of value must be at least 2 for dictionary value @ data[0]['conditions'][0]['or']",
        ),
        (
            {
                "name": "not enought items and",
                "conditions": [{"and": []}],
                "actions": {},
            },
            "length of value must be at least 2 for dictionary value @ data[0]['conditions'][0]['and']",
        ),
    ),
)
def test_invalid_pull_request_rule(invalid, error):
    with pytest.raises(voluptuous.Invalid) as i:
        pull_request_rule_from_list([invalid])

    assert error in str(i.value)


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
        rules.YamlSchema(
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=main
                actions:
                  comment:
                    message: |
                      This pull request has been approved by:
                      {% for name in label %}
                      @{{name}}
                      {% endfor %}
                      {% for name in files %}
                      @{{name}}
                      {% endfor %}
                      {% for name in assignee %}
                      @{{name}}
                      {% endfor %}
                      {% for name in approved_reviews_by %}
                      @{{name}}
                      {% endfor %}
                      Thank you @{{author}} for your contributions!
            """
        )
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]


def test_jinja_filters():
    rules.UserConfigurationSchema(
        rules.YamlSchema(
            """queue_rules:
  - name: default
    conditions: []

pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      queue:
        name: default
        commit_message_template: |
          Merge PR #{{ number }} - {{ title }}
          {{ body | get_section("## Commit Message", "") }}
"""
        )
    )
    rules.UserConfigurationSchema(
        rules.YamlSchema(
            """queue_rules:
  - name: default
    conditions: []

pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      queue:
        name: default
        commit_message_template: |
          Merge PR #{{ number }} - {{ title }}
          {{ body | get_section("## Commit Message") | markdownify }}
"""
        )
    )


def test_jinja_with_wrong_syntax():
    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            rules.YamlSchema(
                """pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      comment:
        message: |
          This pull request has been approved by:
          {% for name in approved_reviews_by %}
          Thank you @{{author}} for your contributions!
"""
            )
        )
    assert str(i.value) == (
        "Template syntax error @ data['pull_request_rules']"
        "[0]['actions']['comment']['message'][line 3]"
    )

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            rules.YamlSchema(
                """pull_request_rules:
  - name: ahah
    conditions:
    - base=main
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
                - base=main
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
                - base=main
                actions:
                  comment:
                    message: |
                      This pull request has been approved by
                      {% for name in approved_reviews_by %}
                      @{{ name }}
                      {% endfor %}
            """
        ),
        (
            """
            defaults:
              actions:
                rebase:
                  bot_account: test-bot-account
            pull_request_rules:
              - name: ahah
                conditions:
                - base=main
                actions:
                  comment:
                    message: |
                      This pull request has been approved by
            """
        ),
        (
            """
            defaults:
              actions:
                comment:
                  message: I love Mergify
                rebase:
                  bot_account: test-bot-account
            pull_request_rules:
              - name: ahah
                conditions:
                - base=main
                actions:
                  comment: {}
                  rebase: {}
            """
        ),
    ),
)
@pytest.mark.asyncio
async def test_get_mergify_config(valid: str, redis_cache: utils.RedisCache) -> None:
    async def item(
        *args: typing.Any, **kwargs: typing.Any
    ) -> github_types.GitHubContentFile:
        return github_types.GitHubContentFile(
            {
                "content": encodebytes(valid.encode()).decode(),
                "path": ".mergify.yml",
                "type": "file",
                "sha": github_types.SHAType("azertyu"),
            }
        )

    client = mock.Mock()
    client.item.return_value = item()

    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("user"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "user/name",
            "name": github_types.GitHubRepositoryName("name"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)

    config_file = await repository.get_mergify_config_file()
    assert config_file is not None
    schema = get_mergify_config(config_file)
    assert isinstance(schema, dict)
    assert "pull_request_rules" in schema


@pytest.mark.asyncio
async def test_get_mergify_config_with_defaults(redis_cache: utils.RedisCache) -> None:

    config = """
defaults:
  actions:
    comment:
      bot_account: foo-bot
    rebase:
      bot_account: test-bot-account
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      comment:
        message: I love Mergify
      rebase: {}
"""

    async def item(
        *args: typing.Any, **kwargs: typing.Any
    ) -> github_types.GitHubContentFile:
        return github_types.GitHubContentFile(
            {
                "content": encodebytes(config.encode()).decode(),
                "path": ".mergify.yml",
                "type": "file",
                "sha": github_types.SHAType("azertyu"),
            }
        )

    client = mock.Mock()
    client.item.return_value = item()

    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("foobar"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )
    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "foobar/xyz",
            "name": github_types.GitHubRepositoryName("xyz"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)
    config_file = await repository.get_mergify_config_file()
    assert config_file is not None

    schema = get_mergify_config(config_file)
    assert isinstance(schema, dict)

    assert len(schema["pull_request_rules"].rules) == 1

    comment = schema["pull_request_rules"].rules[0].actions["comment"].config
    assert comment == {"message": "I love Mergify", "bot_account": "foo-bot"}

    rebase = schema["pull_request_rules"].rules[0].actions["rebase"].config
    assert rebase == {"bot_account": "test-bot-account"}

    config = """
defaults:
  actions:
    comment:
      message: I love Mergify
      bot_account: AutoBot
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      comment:
        message: I really love Mergify
"""

    client = mock.Mock()
    client.item.return_value = item()

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)
    config_file = await repository.get_mergify_config_file()
    assert config_file is not None

    schema = get_mergify_config(config_file)
    assert isinstance(schema, dict)

    assert len(schema["pull_request_rules"].rules) == 1

    comment = schema["pull_request_rules"].rules[0].actions["comment"].config
    assert comment == {"message": "I really love Mergify", "bot_account": "AutoBot"}


@pytest.mark.asyncio
async def test_get_mergify_config_location_from_cache(
    redis_cache: utils.RedisCache,
) -> None:
    client = mock.AsyncMock()
    client.item.side_effect = [
        http.HTTPNotFound("Not Found", request=mock.Mock(), response=mock.Mock()),
        http.HTTPNotFound("Not Found", request=mock.Mock(), response=mock.Mock()),
        github_types.GitHubContentFile(
            {
                "content": encodebytes("whatever".encode()).decode(),
                "type": "file",
                "path": ".github/mergify.yml",
                "sha": github_types.SHAType("zeazeaze"),
            }
        ),
    ]

    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("foobar"),
            "id": github_types.GitHubAccountIdType(0),
            "type": "User",
            "avatar_url": "",
        }
    )
    gh_repo = github_types.GitHubRepository(
        {
            "full_name": "foobar/xyz",
            "name": github_types.GitHubRepositoryName("xyz"),
            "private": False,
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType("ref"),
        }
    )
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)

    await repository.get_mergify_config_file()
    assert client.item.call_count == 3
    client.item.assert_has_calls(
        [
            mock.call("/repos/foobar/xyz/contents/.mergify.yml", params={}),
            mock.call("/repos/foobar/xyz/contents/.mergify/config.yml", params={}),
            mock.call("/repos/foobar/xyz/contents/.github/mergify.yml", params={}),
        ]
    )

    client.item.reset_mock()
    client.item.side_effect = [
        github_types.GitHubContentFile(
            {
                "content": encodebytes("whatever".encode()).decode(),
                "type": "file",
                "path": ".github/mergify.yml",
                "sha": github_types.SHAType("zeazeaze"),
            }
        ),
    ]
    repository._caches = context.RepositoryCaches()
    await repository.get_mergify_config_file()
    assert client.item.call_count == 1
    client.item.assert_has_calls(
        [
            mock.call("/repos/foobar/xyz/contents/.github/mergify.yml", params={}),
        ]
    )


@pytest.mark.parametrize(
    "invalid",
    (
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
@pytest.mark.asyncio
async def test_get_mergify_config_invalid(
    invalid: str, redis_cache: utils.RedisCache
) -> None:
    with pytest.raises(InvalidRules):

        async def item(
            *args: typing.Any, **kwargs: typing.Any
        ) -> github_types.GitHubContentFile:
            return github_types.GitHubContentFile(
                {
                    "content": encodebytes(invalid.encode()).decode(),
                    "path": ".mergify.yml",
                    "type": "file",
                    "sha": github_types.SHAType("azertyu"),
                }
            )

        client = mock.Mock()
        client.item.return_value = item()

        gh_owner = github_types.GitHubAccount(
            {
                "login": github_types.GitHubLogin("foobar"),
                "id": github_types.GitHubAccountIdType(0),
                "type": "User",
                "avatar_url": "",
            }
        )
        gh_repo = github_types.GitHubRepository(
            {
                "full_name": "foobar/xyz",
                "name": github_types.GitHubRepositoryName("xyz"),
                "private": False,
                "id": github_types.GitHubRepositoryIdType(0),
                "owner": gh_owner,
                "archived": False,
                "url": "",
                "html_url": "",
                "default_branch": github_types.GitHubRefType("ref"),
            }
        )
        installation_json = github_types.GitHubInstallation(
            {
                "id": github_types.GitHubInstallationIdType(12345),
                "target_type": gh_owner["type"],
                "permissions": {},
                "account": gh_owner,
            }
        )

        installation = context.Installation(
            installation_json,
            subscription.Subscription(
                redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
            ),
            client,
            redis_cache,
        )
        repository = context.Repository(
            installation,
            gh_repo,
        )

        config_file = await repository.get_mergify_config_file()
        assert config_file is not None
        get_mergify_config(config_file)


def test_user_configuration_schema():
    with pytest.raises(voluptuous.Invalid) as exc_info:
        rules.UserConfigurationSchema(rules.YamlSchema("- no\n* way"))
    assert str(exc_info.value) == "Invalid YAML at [line 2, column 2]"

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            rules.YamlSchema(
                """
                pull_request_rules:
                  - name: ahah
                    key: not really what we expected
                """
            )
        )
    assert (
        str(i.value) == "extra keys not allowed @ data['pull_request_rules'][0]['key']"
    )

    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert str(ir) == (
        "* extra keys not allowed @ pull_request_rules → item 0 → key\n"
        "* required key not provided @ pull_request_rules → item 0 → actions\n"
        "* required key not provided @ pull_request_rules → item 0 → conditions"
    )
    assert [] == ir.get_annotations(".mergify.yml")

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            rules.YamlSchema(
                """invalid:
- *yaml
                """
            )
        )
    assert str(i.value) == "Invalid YAML at [line 2, column 3]"

    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert (
        str(ir)
        == """Invalid YAML @ line 2, column 3
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
            rules.YamlSchema(
                """
                pull_request_rules:
                """
            )
        )
    assert (
        str(i.value)
        == "expected a list for dictionary value @ data['pull_request_rules']"
    )
    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert str(ir) == "expected a list for dictionary value @ pull_request_rules"

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(rules.YamlSchema(""))
    assert str(i.value) == "expected a dictionary"
    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert str(ir) == "expected a dictionary"

    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(
            rules.YamlSchema(
                """
                pull_request_rules:
                  - name: add label
                    conditions:
                      - conflict
                    actions:
                      label:
                        add:
                          - conflict:
                """
            )
        )
    assert (
        str(i.value)
        == "expected str @ data['pull_request_rules'][0]['actions']['label']['add'][0]"
    )
    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert (
        str(ir)
        == "expected str @ pull_request_rules → item 0 → actions → label → add → item 0"
    )


def test_user_binary_file():
    with pytest.raises(voluptuous.Invalid) as i:
        rules.UserConfigurationSchema(rules.YamlSchema(chr(4)))
    assert str(i.value) == "Invalid YAML at []"
    ir = rules.InvalidRules(i.value, ".mergify.yml")
    assert (
        str(ir)
        == """Invalid YAML
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
            r"Invalid condition 'head~=\(lol'. Invalid regular expression: "
            r"missing \), "
            r"unterminated subpattern at position 0 @ ",
        ),
        (
            {"name": "hello", "conditions": ["head|4"], "actions": {}},
            "Invalid condition ",
        ),
        (
            {"name": "hello", "conditions": [{"foo": "bar"}], "actions": {}},
            r"extra keys not allowed @ data\[0\]\['conditions'\]\[0\]\['foo'\]",
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


@pytest.mark.asyncio
async def test_get_pull_request_rule(redis_cache: utils.RedisCache) -> None:

    client = mock.Mock()

    get_reviews = [
        {
            "user": {"login": "sileht", "id": 12321, "type": "User"},
            "state": "APPROVED",
            "author_association": "MEMBER",
        }
    ]
    get_files = [{"filename": "README.rst"}, {"filename": "setup.py"}]
    get_team_members = [{"login": "sileht", "id": 12321}, {"login": "jd", "id": 2644}]

    get_checks: typing.List[github_types.GitHubCheckRun] = []
    get_statuses: typing.List[github_types.GitHubStatus] = [
        {
            "context": "continuous-integration/fake-ci",
            "state": "success",
            "description": "foobar",
            "target_url": "http://example.com",
            "avatar_url": "",
        }
    ]

    async def client_item(url, *args, **kwargs):
        if url == "/repos/another-jd/name/collaborators/sileht/permission":
            return {"permission": "write"}
        elif url == "/repos/another-jd/name/collaborators/jd/permission":
            return {"permission": "write"}
        elif url == "/repos/another-jd/name/branches/main/protection":
            raise http.HTTPNotFound(
                message="boom", response=mock.Mock(), request=mock.Mock()
            )
        raise RuntimeError(f"not handled url {url}")

    client.item.side_effect = client_item

    async def client_items(url, *args, **kwargs):
        if url == "/repos/another-jd/name/pulls/1/reviews":
            for r in get_reviews:
                yield r
        elif url == "/repos/another-jd/name/pulls/1/files":
            for f in get_files:
                yield f
        elif url == "/repos/another-jd/name/commits/<sha>/check-runs":
            for c in get_checks:
                yield c
        elif url == "/repos/another-jd/name/commits/<sha>/status":
            for s in get_statuses:
                yield s
        elif url == "/orgs/another-jd/teams/my-reviewers/members":
            for tm in get_team_members:
                yield tm
        else:
            raise RuntimeError(f"not handled url {url}")

    client.items.side_effect = client_items

    gh_owner = github_types.GitHubAccount(
        {
            "login": github_types.GitHubLogin("another-jd"),
            "id": github_types.GitHubAccountIdType(2644),
            "type": "User",
            "avatar_url": "",
        }
    )
    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(123321),
            "name": github_types.GitHubRepositoryName("name"),
            "full_name": "another-jd/name",
            "private": False,
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "owner": gh_owner,
        }
    )
    installation_json = github_types.GitHubInstallation(
        {
            "id": github_types.GitHubInstallationIdType(12345),
            "target_type": gh_owner["type"],
            "permissions": {},
            "account": gh_owner,
        }
    )

    installation = context.Installation(
        installation_json,
        subscription.Subscription(
            redis_cache, 0, "", frozenset([subscription.Features.PUBLIC_REPOSITORY])
        ),
        client,
        redis_cache,
    )
    repository = context.Repository(installation, gh_repo)
    ctxt = await context.Context.create(
        repository,
        github_types.GitHubPullRequest(
            {
                "node_id": "42",
                "locked": False,
                "assignees": [],
                "requested_reviewers": [],
                "requested_teams": [],
                "milestone": None,
                "id": github_types.GitHubPullRequestId(0),
                "number": github_types.GitHubPullRequestNumber(1),
                "commits": 1,
                "html_url": "<html_url>",
                "merge_commit_sha": None,
                "maintainer_can_modify": True,
                "rebaseable": True,
                "state": "closed",
                "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "closed_at": None,
                "merged_by": None,
                "merged_at": None,
                "merged": False,
                "draft": False,
                "mergeable_state": "unstable",
                "mergeable": True,
                "labels": [],
                "changed_files": 1,
                "base": {
                    "label": "repo",
                    "ref": github_types.GitHubRefType("main"),
                    "repo": gh_repo,
                    "user": gh_owner,
                    "sha": github_types.SHAType("mew"),
                },
                "head": {
                    "label": "foo",
                    "ref": github_types.GitHubRefType("myfeature"),
                    "sha": github_types.SHAType("<sha>"),
                    "repo": gh_repo,
                    "user": gh_owner,
                },
                "title": "My awesome job",
                "body": "",
                "user": {
                    "login": github_types.GitHubLogin("another-jd"),
                    "id": github_types.GitHubAccountIdType(2644),
                    "type": "User",
                    "avatar_url": "",
                },
            }
        ),
    )

    # Empty conditions
    pull_request_rules = rules.PullRequestRules(
        [
            rules.PullRequestRule(
                name="default",
                disabled=None,
                conditions=conditions.PullRequestRuleConditions([]),
                actions={},
            )
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [{"name": "hello", "conditions": ["base:main"], "actions": {}}]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello"]
    assert [r.name for r in match.matching_rules] == ["hello"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["base:main"], "actions": {}},
            {"name": "backport", "conditions": ["base:main"], "actions": {}},
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r in match.matching_rules] == ["hello", "backport"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["author:foobar"], "actions": {}},
            {"name": "backport", "conditions": ["base:main"], "actions": {}},
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r in match.matching_rules] == ["backport"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {"name": "hello", "conditions": ["author:another-jd"], "actions": {}},
            {"name": "backport", "conditions": ["base:main"], "actions": {}},
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["hello", "backport"]
    assert [r.name for r in match.matching_rules] == ["hello", "backport"]
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

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["merge"]
    assert [r.name for r in match.matching_rules] == []

    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=main",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            }
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["merge"]
    assert [r.name for r in match.matching_rules] == ["merge"]
    for rule in match.rules:
        assert rule.actions == {}

    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "merge",
                "conditions": [
                    "base=main",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            },
            {
                "name": "fast merge",
                "conditions": [
                    "base=main",
                    "label=fast-track",
                    "check-success=continuous-integration/fake-ci",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge with alternate ci",
                "conditions": [
                    "base=main",
                    {
                        "or": [
                            {
                                "and": [
                                    "label=automerge",
                                    "label=ready",
                                ]
                            },
                            "label=fast-track",
                        ]
                    },
                    "check-success=continuous-integration/fake-ci-bis",
                    "#approved-reviews-by>=1",
                ],
                "actions": {},
            },
            {
                "name": "fast merge from a bot",
                "conditions": [
                    "base=main",
                    {
                        "or": [
                            "label=python-deps",
                            "label=node-deps",
                        ]
                    },
                    "author=mybot",
                    "check-success=continuous-integration/fake-ci",
                ],
                "actions": {},
            },
        ]
    )
    match = await pull_request_rules.get_pull_request_rule(ctxt)

    assert [r.name for r in match.rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
        "fast merge from a bot",
    ]
    assert [r.name for r in match.matching_rules] == [
        "merge",
        "fast merge",
        "fast merge with alternate ci",
    ]
    for rule in match.rules:
        assert rule.actions == {}

    assert match.matching_rules[0].name == "merge"
    assert not match.matching_rules[0].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 1
    assert str(missing_conditions[0]) == "#approved-reviews-by>=2"

    assert match.matching_rules[1].name == "fast merge"
    assert not match.matching_rules[1].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[1].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 1
    assert str(missing_conditions[0]) == "label=fast-track"

    assert match.matching_rules[2].name == "fast merge with alternate ci"
    assert not match.matching_rules[2].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[2].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 4
    assert str(missing_conditions[0]) == "label=automerge"
    assert str(missing_conditions[1]) == "label=ready"
    assert str(missing_conditions[2]) == "label=fast-track"
    assert (
        str(missing_conditions[3]) == "check-success=continuous-integration/fake-ci-bis"
    )

    # Team conditions with one review missing
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "default",
                "conditions": [
                    "approved-reviews-by=@another-jd/my-reviewers",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            }
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]

    assert match.matching_rules[0].name == "default"
    assert not match.matching_rules[0].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 1
    assert str(missing_conditions[0]) == "#approved-reviews-by>=2"

    get_reviews.append(
        {
            "user": {"login": "jd", "id": 2644, "type": "User"},
            "state": "APPROVED",
            "author_association": "MEMBER",
        }
    )

    ctxt._caches.reviews.delete()
    ctxt._caches.consolidated_reviews.delete()

    # Team conditions with no review missing
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "default",
                "conditions": [
                    "approved-reviews-by=@another-jd/my-reviewers",
                    "#approved-reviews-by>=2",
                ],
                "actions": {},
            }
        ]
    )

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]

    assert match.matching_rules[0].name == "default"
    assert match.matching_rules[0].conditions.match

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

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]
    assert match.matching_rules[0].name == "default"
    assert match.matching_rules[0].conditions.match

    # Forbidden labels, when forbiden label set
    ctxt.pull["labels"] = [
        {"id": 0, "color": "#1234", "default": False, "name": "status/wip"}
    ]

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]
    assert match.matching_rules[0].name == "default"
    assert not match.matching_rules[0].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 1
    assert str(missing_conditions[0]) == (
        "-label~=^(status/wip|status/blocked|review/need2)$"
    )

    # Forbidden labels, when other label set
    ctxt.pull["labels"] = [
        {"id": 0, "color": "#1234", "default": False, "name": "allowed"}
    ]

    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]
    assert match.matching_rules[0].name == "default"
    assert match.matching_rules[0].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 0

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
    match = await pull_request_rules.get_pull_request_rule(ctxt)
    assert [r.name for r in match.rules] == ["default"]
    assert [r.name for r in match.matching_rules] == ["default"]
    assert match.matching_rules[0].name == "default"
    assert match.matching_rules[0].conditions.match
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]

    # branch protection
    async def client_item_with_branch_protection_enabled(url, *args, **kwargs):
        if url == "/repos/another-jd/name/branches/main/protection":
            return {
                "required_status_checks": {"contexts": ["awesome-ci"]},
                "required_linear_history": {"enabled": False},
            }
        raise RuntimeError(f"not handled url {url}")

    client.item.side_effect = client_item_with_branch_protection_enabled
    ctxt.repository._caches.branch_protections.clear()
    pull_request_rules = pull_request_rule_from_list(
        [
            {
                "name": "default",
                "conditions": [],
                "actions": {"merge": {}, "comment": {"message": "yo"}},
            }
        ]
    )
    match = await pull_request_rules.get_pull_request_rule(ctxt)

    assert [r.name for r in match.rules] == ["default", "default"]
    assert list(match.matching_rules[0].actions.keys()) == ["merge"]
    assert len(match.matching_rules[0].conditions.condition.conditions) == 1
    assert not match.matching_rules[0].conditions.match
    group = match.matching_rules[0].conditions.condition.conditions[0]
    assert isinstance(group, rules.conditions.RuleConditionGroup)
    assert group.operator == "or"
    assert len(group.conditions) == 3
    assert str(group.conditions[0]) == "check-success=awesome-ci"
    assert str(group.conditions[1]) == "check-neutral=awesome-ci"
    assert str(group.conditions[2]) == "check-skipped=awesome-ci"
    missing_conditions = [
        c for c in match.matching_rules[0].conditions.walk() if not c.match
    ]
    assert len(missing_conditions) == 3
    assert str(missing_conditions[0]) == "check-success=awesome-ci"
    assert str(missing_conditions[1]) == "check-neutral=awesome-ci"
    assert str(missing_conditions[2]) == "check-skipped=awesome-ci"
    assert list(match.matching_rules[1].actions.keys()) == ["comment"]
    assert len(match.matching_rules[1].conditions.condition.conditions) == 0


def test_check_runs_custom():
    pull_request_rules = rules.UserConfigurationSchema(
        rules.YamlSchema(
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=main
                actions:
                  post_check:
                    title: '{{ check_rule_name }} whatever'
                    summary: |
                      This pull request has been checked!
                      Thank you @{{author}} for your contributions!

                      {{ check_conditions }}
            """
        )
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]


def test_check_runs_default():
    pull_request_rules = rules.UserConfigurationSchema(
        rules.YamlSchema(
            """
            pull_request_rules:
              - name: ahah
                conditions:
                - base=main
                actions:
                  post_check: {}
            """
        )
    )["pull_request_rules"]
    assert [rule.name for rule in pull_request_rules] == [
        "ahah",
    ]


def test_merge_config():
    config = {
        "defaults": {"actions": {"rebase": {"bot_account": "foo"}}},
        "pull_request_rules": [
            {
                "name": "hello",
                "conditions": ["head:main"],
                "actions": {"rebase": {}},
            }
        ],
    }

    defaults = config.pop("defaults", {})
    merged_config = rules.merge_config(config, defaults)

    expected_config = config.copy()
    expected_config["pull_request_rules"][0]["actions"].update(defaults["actions"])

    assert merged_config == expected_config

    config = {
        "defaults": {
            "actions": {
                "rebase": {"bot_account": "foo"},
                "comment": {"message": "Hello World!"},
            }
        },
        "pull_request_rules": [
            {
                "name": "hello",
                "conditions": ["head:main"],
                "actions": {"rebase": {"bot_account": "bar"}},
            }
        ],
    }

    defaults = config.pop("defaults", {})
    merged_config = rules.merge_config(config, defaults)

    assert merged_config == config

    config = {
        "pull_request_rules": [
            {
                "name": "hello",
                "conditions": ["head:main"],
                "actions": {"rebase": {"bot_account": "bar"}},
            }
        ],
    }

    defaults = config.pop("defaults", {})
    merged_config = rules.merge_config(config, defaults)

    assert merged_config == config


def test_actions_with_options_none():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
defaults:
  actions:
    post_check:
    rebase:
    queue:
      name: default
    comment:
      bot_account: "foobar"
queue_rules:
    - name: default
      conditions: []
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      comment:
      rebase:
        bot_account: "foobar"
      post_check:
      queue:
            """,
    )

    config = rules.get_mergify_config(file)

    assert [list(rule.actions.keys()) for rule in config["pull_request_rules"]][0] == [
        "comment",
        "rebase",
        "post_check",
        "queue",
    ]


def test_action_queue_with_duplicate_queue():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
queue_rules:
  - name: default
    conditions: []
  - name: default
    conditions: []
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      queue:
        name: default
""",
    )

    with pytest.raises(rules.InvalidRules) as e:
        rules.get_mergify_config(file)

    assert (
        str(e.value.error)
        == "queue_rules names must be unique, found `default` twice for dictionary value @ data['queue_rules']"
    )


def test_action_queue_with_no_default_queue():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      queue:
        name: missing
            """,
    )

    with pytest.raises(rules.InvalidRules) as e:
        rules.get_mergify_config(file)

    assert str(e.value.error) == "missing queue not found"

    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
pull_request_rules:
  - name: ahah
    conditions:
    - base=main
    actions:
      queue:
            """,
    )

    with pytest.raises(rules.InvalidRules) as e:
        rules.get_mergify_config(file)

    assert (
        str(e.value.error)
        == "required key not provided @ data['pull_request_rules'][0]['actions']['queue']['name']"
    )


def test_default_with_no_pull_requests_rules():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
defaults:
  actions:
    merge:
       strict: "smart"
""",
    )

    assert rules.get_mergify_config(file)
    config = rules.get_mergify_config(file)

    assert config["pull_request_rules"].rules == []


def test_multiple_cascaded_errors():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
pull_request_rules:
  - name: automatic merge for Dependabot pull requests
    conditions:
    - author=dependabot[bot]
      - status-success=Travis CI - Pull Request
    actions:
    merge:
        method: merge
""",
    )

    with pytest.raises(rules.InvalidRules) as e:
        rules.get_mergify_config(file)

    assert (
        str(e.value)
        == """* Invalid condition 'author=dependabot[bot] - status-success=Travis CI - Pull Request'. Invalid GitHub login @ pull_request_rules → item 0 → conditions → item 0
```
Invalid GitHub login
```
* expected a dictionary for dictionary value @ pull_request_rules → item 0 → actions
* extra keys not allowed @ pull_request_rules → item 0 → merge"""
    )


@pytest.mark.asyncio
async def test_queue_rules_summary():
    schema = voluptuous.Schema(
        voluptuous.All(
            [voluptuous.Coerce(rules.RuleConditionSchema)],
            voluptuous.Coerce(conditions.QueueRuleConditions),
        )
    )

    c = schema(
        [
            "base=main",
            {"or": ["head=feature-1", "head=feature-2", "head=feature-3"]},
            {"or": ["label=urgent", "status-failure!=noway"]},
            {"or": ["label=bar", "check-success-or-neutral=first-ci"]},
            {"or": ["label=foo", "check-success-or-neutral!=first-ci"]},
            {"and": ["label=foo", "check-success-or-neutral=first-ci"]},
            {"and": ["label=foo", "check-success-or-neutral!=first-ci"]},
            "current-year=2018",
        ]
    )
    c.condition.conditions.extend(
        [
            conditions.RuleCondition(
                "check-success-or-neutral=my-awesome-ci",
                description="🛡 GitHub branch protection",
            ),
            conditions.RuleCondition(
                "author=me",
                description="Another mechanism to get condtions",
            ),
        ]
    )

    pulls = [
        FakeQueuePullRequest(
            {
                "number": 1,
                "current-year": date.Year(2018),
                "author": "me",
                "base": "main",
                "head": "feature-1",
                "label": ["foo", "bar"],
                "check-success-or-neutral": ["first-ci", "my-awesome-ci"],
                "status-failure": ["noway"],
            }
        ),
        FakeQueuePullRequest(
            {
                "number": 2,
                "current-year": date.Year(2018),
                "author": "me",
                "base": "main",
                "head": "feature-2",
                "label": ["foo", "urgent"],
                "check-success-or-neutral": ["first-ci", "my-awesome-ci"],
                "status-failure": ["noway"],
            }
        ),
        FakeQueuePullRequest(
            {
                "number": 3,
                "current-year": date.Year(2018),
                "author": "not-me",
                "base": "main",
                "head": "feature-3",
                "label": ["foo", "urgent"],
                "check-success-or-neutral": ["first-ci", "my-awesome-ci"],
                "status-failure": ["noway"],
            }
        ),
    ]
    await c(pulls)

    assert (
        c.get_summary()
        == """- [X] `base=main`
- [X] any of:
  - `head=feature-1`
    - [X] #1
    - [ ] #2
    - [ ] #3
  - `head=feature-2`
    - [ ] #1
    - [X] #2
    - [ ] #3
  - `head=feature-3`
    - [ ] #1
    - [ ] #2
    - [X] #3
- [ ] any of:
  - `label=urgent`
    - [ ] #1
    - [X] #2
    - [X] #3
  - [ ] `status-failure!=noway`
- [X] any of:
  - `label=bar`
    - [X] #1
    - [ ] #2
    - [ ] #3
  - [X] `check-success-or-neutral=first-ci`
- [X] any of:
  - `label=foo`
    - [X] #1
    - [X] #2
    - [X] #3
  - [ ] `check-success-or-neutral!=first-ci`
- [X] all of:
  - `label=foo`
    - [X] #1
    - [X] #2
    - [X] #3
  - [X] `check-success-or-neutral=first-ci`
- [ ] all of:
  - `label=foo`
    - [X] #1
    - [X] #2
    - [X] #3
  - [ ] `check-success-or-neutral!=first-ci`
- [X] `current-year=2018`
- [X] `check-success-or-neutral=my-awesome-ci` [🛡 GitHub branch protection]
- `author=me` [Another mechanism to get condtions]
  - [X] #1
  - [X] #2
  - [ ] #3
"""
    )


@pytest.mark.asyncio
@freeze_time("2021-09-22T08:00:05", tz_offset=0)
async def test_rules_conditions_schedule():
    pulls = [
        FakeQueuePullRequest(
            {
                "number": 1,
                "author": "me",
                "base": "main",
                "current-timestamp": date.utcnow(),
                "current-time": date.utcnow(),
                "current-day": date.Day(22),
                "current-month": date.Month(9),
                "current-year": date.Year(2021),
                "current-day-of-week": date.DayOfWeek(3),
            }
        ),
    ]
    schema = voluptuous.Schema(
        voluptuous.All(
            [voluptuous.Coerce(rules.RuleConditionSchema)],
            voluptuous.Coerce(conditions.QueueRuleConditions),
        )
    )

    c = schema(
        [
            "base=main",
            "schedule=MON-FRI 08:00-17:00",
            "schedule=MONDAY-FRIDAY 10:00-12:00",
            "schedule=SAT-SUN 07:00-12:00",
        ]
    )

    await c(pulls)

    assert (
        c.get_summary()
        == """- [X] `base=main`
- [X] `schedule=MON-FRI 08:00-17:00`
- [ ] `schedule=MONDAY-FRIDAY 10:00-12:00`
- [ ] `schedule=SAT-SUN 07:00-12:00`
"""
    )


def test_queue_action_defaults():
    file = context.MergifyConfigFile(
        type="file",
        content="whatever",
        sha="azertyuiop",
        path="whatever",
        decoded_content="""
defaults:
  actions:
    queue:
      method: squash

queue_rules:
- name: default
  conditions: []

pull_request_rules:
- name: ahah
  conditions: []
  actions:
    queue:
      name: default
""",
    )

    pull_request_rules = list(rules.get_mergify_config(file)["pull_request_rules"])
    print(pull_request_rules)
    assert pull_request_rules[0].actions["queue"].config["name"] == "default"
    assert pull_request_rules[0].actions["queue"].config["method"] == "squash"
