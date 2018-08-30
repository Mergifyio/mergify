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

import copy
from unittest import mock

import pytest

import voluptuous

import yaml

from mergify_engine import mergify_pull
from mergify_engine import rules
from mergify_engine.rules import filter


with open(rules.default_rule, "r") as f:
    print(f.read())
    f.seek(0)
    DEFAULT_CONFIG = yaml.safe_load(f.read())


def validate_with_get_branch_rule(config, branch="master"):
    return rules.get_branch_rule(config['rules'], branch)


def test_config():
    config = {
        "rules": {
            "default": DEFAULT_CONFIG,
            "branches": {
                "stable/.*": DEFAULT_CONFIG,
                "stable/3.1": DEFAULT_CONFIG,
                "stable/foo": {
                    "automated_backport_labels": {
                        'bp-3.1': 'stable/3.1',
                        'bp-3.2': 'stable/4.2',
                    }
                }
            }
        }
    }
    validate_with_get_branch_rule(config)
    validate_with_get_branch_rule(config, "stable/3.1")
    validate_with_get_branch_rule(config, "stable/foo")


def test_config_default_none():
    config = {
        'rules': {
            'default': None,
            'branches': {
                'master': {
                    'protection': {
                        'required_pull_request_reviews': {
                            'required_approving_review_count': 1,
                            'dismiss_stale_reviews': False,
                            'require_code_owner_reviews': False,
                        },
                        'required_status_checks': {
                            'strict': True,
                            'contexts': [
                                'continuous-integration/travis-ci',
                                'continuous-integration/appveyor/branch',
                            ]
                        },
                        'enforce_admins': False,
                    }
                },
                'beta': {
                    'protection': {
                        'required_pull_request_reviews': {
                            'required_approving_review_count': 1,
                            'dismiss_stale_reviews': False,
                            'require_code_owner_reviews': False,
                        },
                        'required_status_checks': {
                            'strict': True,
                            'contexts': [
                                'continuous-integration/travis-ci',
                                'continuous-integration/appveyor/branch',
                            ]
                        },
                        'enforce_admins': False,
                    }
                }
            }
        }
    }
    validate_with_get_branch_rule(config)
    validate_with_get_branch_rule(config, "beta")


def test_defaulpts_get_branch_rule():
    validate_with_get_branch_rule({"rules": None})


def test_disabled_branch():
    config = {
        "rules": {
            "default": copy.deepcopy(DEFAULT_CONFIG),
            "branches": {
                "^feature/.*": None
            }
        }
    }
    assert validate_with_get_branch_rule(config, "feature/foobar") is None


def test_disabled_default():
    config = {
        "rules": {
            "default": None,
            "branches": {
                "master": copy.deepcopy(DEFAULT_CONFIG),
            }
        }
    }
    assert validate_with_get_branch_rule(config, "feature/foobar") is None
    assert validate_with_get_branch_rule(config, "master") is not None


def test_invalid_yaml():
    fake_repo = mock.Mock()
    fake_repo.get_contents.return_value = mock.Mock(
        decoded_content="  ,;  dkqjshdmlksj\nhkqlsjdh\n-\n  qsjkdlkq\n")
    with pytest.raises(rules.InvalidRules) as excinfo:
        rules.get_mergify_config(fake_repo, "master")
    assert ('Mergify configuration is invalid: position (1:3)' in
            str(excinfo.value))


def test_disabling_files():
    config = {
        "rules": {
            "default": copy.deepcopy(DEFAULT_CONFIG),
        }
    }
    parsed = validate_with_get_branch_rule(config)
    assert parsed["disabling_files"] == [".mergify.yml"]

    config["rules"]["default"]["disabling_files"] = ["foobar.json"]
    parsed = validate_with_get_branch_rule(config)
    assert len(parsed["disabling_files"]) == 2
    assert "foobar.json" in parsed["disabling_files"]
    assert ".mergify.yml" in parsed["disabling_files"]

    with pytest.raises(rules.InvalidRules):
        config["rules"]["default"]["disabling_files"] = None
        validate_with_get_branch_rule(config)


def test_merge_strategy():
    config = {
        "rules": {
            "default": copy.deepcopy(DEFAULT_CONFIG),
        }
    }
    parsed = validate_with_get_branch_rule(config)
    assert parsed["merge_strategy"]["method"] == "merge"

    config["rules"]["default"]["merge_strategy"]["method"] = "squash"
    config["rules"]["default"]["merge_strategy"]["rebase_fallback"] = "squash"
    parsed = validate_with_get_branch_rule(config)
    assert parsed["merge_strategy"]["method"] == "squash"
    assert parsed["merge_strategy"]["rebase_fallback"] == "squash"

    config["rules"]["default"]["merge_strategy"]["method"] = "rebase"
    config["rules"]["default"]["merge_strategy"]["rebase_fallback"] = "none"
    parsed = validate_with_get_branch_rule(config)
    assert parsed["merge_strategy"]["method"] == "rebase"
    assert parsed["merge_strategy"]["rebase_fallback"] == "none"

    with pytest.raises(rules.InvalidRules):
        config["rules"]["default"]["merge_strategy"]["method"] = "foobar"
        validate_with_get_branch_rule(config)


def test_automated_backport_labels():
    config = {
        "rules": {
            "default": {
                "automated_backport_labels": {"foo": "bar"}
            }
        }
    }
    validate_with_get_branch_rule(config)

    config = {
        "rules": {
            "default": {
                "automated_backport_labels": "foo"
            }
        }
    }
    with pytest.raises(rules.InvalidRules):
        validate_with_get_branch_rule(config)


def test_restrictions():
    validate_with_get_branch_rule({
        "rules": {
            "default": {
                "protection": {
                    "restrictions": {},
                },
            },
        },
    })
    validate_with_get_branch_rule({
        "rules": {
            "default": {
                "protection": {
                    "restrictions": None,
                },
            },
        },
    })
    validate_with_get_branch_rule({
        "rules": {
            "default": {
                "protection": {
                    "restrictions": {
                        "teams": ["foobar"],
                    },
                },
            },
        },
    })
    validate_with_get_branch_rule({
        "rules": {
            "default": {
                "protection": {
                    "restrictions": {
                        "users": ["foo", "bar"],
                    }
                }
            }
        }
    })
    validate_with_get_branch_rule({
        "rules": {
            "default": {
                "protection": {
                    "restrictions": {
                        "users": ["foo", "bar"],
                        "teams": ["foobar"],
                    },
                },
            },
        },
    })


def test_partial_required_status_checks():
    config = {
        "rules": {
            "default": {
                "protection": {
                    "required_status_checks": {
                        "strict": True
                    }
                }
            }
        }
    }
    parsed = validate_with_get_branch_rule(config)
    assert parsed["protection"]["required_status_checks"]["contexts"] == []

    config = {
        "rules": {
            "default": {
                "protection": {
                    "required_status_checks": {
                        "contexts": ["foo"]
                    }
                }
            }
        }
    }
    parsed = validate_with_get_branch_rule(config)
    assert parsed["protection"]["required_status_checks"]["strict"] is False


def test_review_count_range():
    config = {
        "rules": {
            "default": {
                "protection": {
                    "required_pull_request_reviews": {
                        "required_approving_review_count": 2
                    }
                }
            }
        }
    }
    validate_with_get_branch_rule(config)

    config = {
        "rules": {
            "default": {
                "protection": {
                    "required_pull_request_reviews": {
                        "required_approving_review_count": -1
                    }
                }
            }
        }
    }
    with pytest.raises(rules.InvalidRules):
        validate_with_get_branch_rule(config)

    config = {
        "rules": {
            "default": {
                "protection": {
                    "required_pull_request_reviews": {
                        "required_approving_review_count": 10
                    }
                }
            }
        }
    }
    with pytest.raises(rules.InvalidRules):
        validate_with_get_branch_rule(config)


def test_pull_request_rule():
    for valid in ({
            "name": "hello",
            "conditions": [
                "head:master",
            ],
    }, {
        "name": "hello",
        "conditions": [
            {"or": ["head:master", {"and": ["base:foo", "base:baz"]}]},
        ],
    }):
        rules.PullRequestRules([valid])


def test_pull_request_rule_schema_invalid():
    for invalid, match in (
            ({
                "name": "hello",
                "conditions": [
                    "this is wrong"
                ],
            }, "Invalid condition "),
            ({
                "name": "hello",
                "conditions": [
                    "head|4"
                ],
            }, "Invalid condition "),
            ({
                "name": "hello",
                "conditions": [
                    {"foo": "bar"}
                ]
            }, "extra keys not allowed"),
    ):
        with pytest.raises(voluptuous.MultipleInvalid, match=match):
            rules.PullRequestRules([invalid])


def test_get_pull_request_rule():
    g_pull = mock.Mock()
    g_pull.assignees = []
    g_pull.labels = []
    g_pull.get_review_requests.return_value = ([], [])
    g_pull.author = "jd"
    g_pull.base.label = "master"
    g_pull.head.label = "myfeature"
    g_pull._rawData = {'locked': False}
    g_pull.title = "My awesome job"
    g_pull.body = "I rock"
    file1 = mock.Mock()
    file1.filename = "README.rst"
    file2 = mock.Mock()
    file2.filename = "setup.py"
    g_pull.get_files.return_value = [file1, file2]

    review = mock.Mock()
    review.user.login = "sileht"
    review.state = "APPROVED"
    review._rawData = {"author_association": "MEMBER"}
    g_pull.get_reviews.return_value = [review]

    pull_request = mergify_pull.MergifyPull(g_pull=g_pull,
                                            installation_id=123)
    fake_ci = mock.Mock()
    fake_ci.context = "continuous-integration/fake-ci"
    fake_ci.state = "success"
    pull_request._get_checks = mock.Mock()
    pull_request._get_checks.return_value = [fake_ci]

    # Empty conditions
    pull_request_rules = rules.PullRequestRules([{
        "name": "default",
        "conditions": [],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["default"]
    assert [r['name'] for r in match.matching_rules] == ["default"]
    assert match.rules == match.matching_rules
    assert match.next_rules == []
    assert match.rule == {}

    pull_request_rules = rules.PullRequestRules([{
        "name": "hello",
        "conditions": [
            "base:master",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["hello"]
    assert [r['name'] for r in match.matching_rules] == ["hello"]
    assert match.rules == match.matching_rules
    assert match.next_rules == []
    assert match.rule == {}

    pull_request_rules = rules.PullRequestRules([{
        "name": "hello",
        "conditions": [
            "base:master",
        ],
    }, {
        "name": "backport",
        "conditions": [
            "base:master",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["hello", "backport"]
    assert [r['name'] for r in match.matching_rules] == ["hello", "backport"]
    assert match.rules == match.matching_rules
    assert match.next_rules == []
    assert match.rule == {}

    pull_request_rules = rules.PullRequestRules([{
        "name": "hello",
        "conditions": [
            "#files=3",
        ],
    }, {
        "name": "backport",
        "conditions": [
            "base:master",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["hello", "backport"]
    assert [r['name'] for r in match.matching_rules] == ["backport"]
    assert match.next_rules[0][0]['name'] == "hello"
    assert match.next_rules[0][1] == filter.Filter.parse("#files=3")
    assert match.rule == {}

    pull_request_rules = rules.PullRequestRules([{
        "name": "hello",
        "conditions": [
            "#files=2",
        ],
    }, {
        "name": "backport",
        "conditions": [
            "base:master",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["hello", "backport"]
    assert [r['name'] for r in match.matching_rules] == ["hello", "backport"]
    assert match.rules == match.matching_rules
    assert match.next_rules == []
    assert match.rule == {}

    # No match
    pull_request_rules = rules.PullRequestRules([{
        "name": "merge",
        "conditions": [
            "base=xyz",
            "status-success=continuous-integration/fake-ci",
            "#review-approved-by>=1",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["merge"]
    assert [r['name'] for r in match.matching_rules] == []
    assert match.next_rules[0][0]['name'] == "merge"
    assert match.next_rules[0][1] == filter.Filter.parse("base=xyz")
    assert match.rule == {}

    pull_request_rules = rules.PullRequestRules([{
        "name": "merge",
        "conditions": [
            "base=master",
            "status-success=continuous-integration/fake-ci",
            "#review-approved-by>=1",
        ],
    }])

    match = pull_request_rules.get_pull_request_rule(pull_request)
    assert [r['name'] for r in match.rules] == ["merge"]
    assert [r['name'] for r in match.matching_rules] == ["merge"]
    assert match.rules == match.matching_rules
    assert match.next_rules == []
    assert match.rule == {}
