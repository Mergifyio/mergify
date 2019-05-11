# -*- encoding: utf-8 -*-
#
# Copyright © 2019 Mergify
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
import pytest

import voluptuous


from mergify_engine import rules


def test_valid_condition():
    c = rules.PullRequestRuleCondition("head~=bar")
    assert str(c) == "head~=bar"


def test_invalid_condition_re():
    with pytest.raises(voluptuous.Invalid):
        rules.PullRequestRuleCondition("head~=(bar")
