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

import copy
from unittest import mock

import pytest

import yaml

from mergify_engine import rules

with open("default_rule.yml", "r") as f:
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
