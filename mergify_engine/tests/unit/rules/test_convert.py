# -*- encoding: utf-8 -*-
#
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
from mergify_engine import rules
from mergify_engine.rules import convert


def test_convert_simple():
    old_rules = {
        'rules': {
            'default': {
                'automated_backport_labels': {
                    'backport-to-3.1': 'stable/3.1',
                    'backport-to-3.0': 'stable/3.0',
                },
                'protection': {
                    'required_pull_request_reviews': {
                        'required_approving_review_count': 2
                    },
                    'required_status_checks': {
                        'contexts': ['continuous-integration/travis-ci'],
                        'strict': True
                    }},
                'merge_strategy': {
                    'method': 'rebase'
                }},
            'branches': {
                '^stable/.*': {
                    'protection': {
                        'required_pull_request_reviews': {
                            'required_approving_review_count': 1
                        }
                    }
                }
            }
        }
    }
    converted = convert.convert_config(old_rules["rules"])
    assert converted == [
        {
            "name": "default",
            "conditions": ["-base~=^stable/.*",
                           "label!=no-mergify",
                           "#approved-reviews-by>=2",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                    "strict": True,
                },
            },
        },
        {
            "name": "backport stable/3.0",
            "conditions": ["-base~=^stable/.*",
                           "label!=no-mergify",
                           "label=backport-to-3.0"],
            "actions": {
                "backport": {
                    "branches": ["stable/3.0"],
                },
            },
        },
        {
            "name": "backport stable/3.1",
            "conditions": ["-base~=^stable/.*",
                           "label!=no-mergify",
                           "label=backport-to-3.1"],
            "actions": {
                "backport": {
                    "branches": ["stable/3.1"],
                },
            },
        },
        {
            "name": "^stable/.* branch",
            "conditions": ["base~=^stable/.*",
                           "label!=no-mergify",
                           "#approved-reviews-by>=1",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                    "strict": True,
                },
            },
        },
        {
            "name": "backport stable/3.0 from ^stable/.*",
            "conditions": ["base~=^stable/.*",
                           "label!=no-mergify",
                           "label=backport-to-3.0"],
            "actions": {
                "backport": {
                    "branches": ["stable/3.0"],
                },
            }
        },
        {
            "name": "backport stable/3.1 from ^stable/.*",
            "conditions": ["base~=^stable/.*",
                           "label!=no-mergify",
                           "label=backport-to-3.1"],
            "actions": {
                "backport": {
                    "branches": ["stable/3.1"],
                },
            },
        },

    ]
    # Validate generated conf with the schema
    rules.PullRequestRules(converted)


def test_convert_no_branches():
    old_rules = {
        'rules': {
            'default': {
                'protection': {
                    'required_pull_request_reviews': {
                        'required_approving_review_count': 2
                    },
                    'required_status_checks': {
                        'contexts': ['continuous-integration/travis-ci'],
                        'strict': True
                    }},
                'merge_strategy': {
                    'method': 'rebase'
                }},
        }
    }
    converted = convert.convert_config(old_rules["rules"])
    assert converted == [
        {
            "name": "default",
            "conditions": ["label!=no-mergify",
                           "#approved-reviews-by>=2",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                    "strict": True,
                },
            },
        },
    ]
    # Validate generated conf with the schema
    rules.PullRequestRules(converted)


def test_convert_rebase_fallback():
    old_rules = {
        'rules': {
            'default': {
                'protection': {
                    'required_pull_request_reviews': {
                        'required_approving_review_count': 2
                    },
                    'required_status_checks': {
                        'contexts': ['continuous-integration/travis-ci'],
                        'strict': True
                    }},
                'merge_strategy': {
                    'method': 'rebase',
                    'rebase_fallback': "none",
                }},
            'branches': {
                '^stable/.*': {
                    'merge_strategy': {
                        'method': 'rebase',
                        'rebase_fallback': "merge",
                    },
                },
                '^unstable/.*': {
                    'merge_strategy': {
                        'method': 'rebase',
                        'rebase_fallback': "none",
                    },
                },
            },
        },
    }
    converted = convert.convert_config(old_rules["rules"])
    assert converted == [
        {
            "name": "default",
            "conditions": ["-base~=^stable/.*",
                           "-base~=^unstable/.*",
                           "label!=no-mergify",
                           "#approved-reviews-by>=2",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": None,
                    "strict": True,
                },
            },
        },
        {
            "name": "^stable/.* branch",
            "conditions": ["base~=^stable/.*",
                           "label!=no-mergify",
                           "#approved-reviews-by>=2",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                    "strict": True,
                },
            },
        },
        {
            "name": "^unstable/.* branch",
            "conditions": ["base~=^unstable/.*",
                           "label!=no-mergify",
                           "#approved-reviews-by>=2",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": None,
                    "strict": True,
                },
            },
        },
    ]
    # Validate generated conf with the schema
    rules.PullRequestRules(converted)


def test_convert_with_some_none():
    old_rules = {
        'rules': {
            'default': None,
            'branches': {
                '^stable/.*': {
                    'protection': None,
                    'merge_strategy': {
                        'method': 'rebase',
                        'rebase_fallback': "merge",
                    },
                },
                '^unstable/.*': {
                    'required_pull_request_reviews': None,
                    'required_status_checks': None,
                },
            },
        },
    }
    converted = convert.convert_config(old_rules["rules"])
    assert converted == [
        {
            "name": "^stable/.* branch",
            "conditions": ["base~=^stable/.*",
                           "label!=no-mergify"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                },
            },
        },
        {
            "name": "^unstable/.* branch",
            "conditions": ["base~=^unstable/.*",
                           "label!=no-mergify",
                           '#approved-reviews-by>=1'],
            "actions": {
                "merge": {
                    "method": "merge",
                    "rebase_fallback": "merge",
                },
            }
        }
    ]
    # Validate generated conf with the schema
    rules.PullRequestRules(converted)


def test_null_branch():
    old_rules = {
        'rules': {
            'default': {
                'protection': {
                    'required_pull_request_reviews': {
                        'required_approving_review_count': 1
                    },
                    'required_status_checks': {
                        'contexts': ['continuous-integration/travis-ci'],
                    }},
                'merge_strategy': {
                    'method': 'rebase',
                }},
            'branches': {
                'gh-pages': None,
            },
        },
    }
    converted = convert.convert_config(old_rules["rules"])
    assert converted == [
        {
            "name": "default",
            "conditions": ["-base=gh-pages",
                           "label!=no-mergify",
                           "#approved-reviews-by>=1",
                           "status-success=continuous-integration/travis-ci"],
            "actions": {
                "merge": {
                    "method": "rebase",
                    "rebase_fallback": "merge",
                },
            },
        },
    ]
    # Validate generated conf with the schema
    rules.PullRequestRules(converted)
