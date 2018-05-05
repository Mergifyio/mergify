# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
import mock
import pytest
import yaml

from mergify_engine import rules

with open("default_rule.yml", "r") as f:
    print(f.read())
    f.seek(0)
    DEFAULT_CONFIG = yaml.load(f.read())


def validate_with_get_branch_rule(config, branch="master"):
    fake_pr = mock.Mock(base=mock.Mock(ref="master"))
    fake_repo = mock.Mock()
    fake_repo.get_contents.return_value = mock.Mock(
        decoded_content=yaml.dump(config))
    rules.get_branch_rule(fake_repo, fake_pr)


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


def test_defauls_get_branch_rule():
    validate_with_get_branch_rule({"rules": None})


def test_invalid_yaml():
    fake_pr = mock.Mock(base=mock.Mock(ref="master"))
    fake_repo = mock.Mock()
    fake_repo.get_contents.return_value = mock.Mock(
        decoded_content="  ,;  dkqjshdmlksj\nhkqlsjdh\n-\n  qsjkdlkq\n")
    with pytest.raises(rules.NoRules) as excinfo:
        rules.get_branch_rule(fake_repo, fake_pr)
    assert '.mergify.yml is invalid at position: (1:3)' in str(excinfo.value)


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
    with pytest.raises(rules.NoRules):
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
    with pytest.raises(rules.NoRules):
        validate_with_get_branch_rule(config)
