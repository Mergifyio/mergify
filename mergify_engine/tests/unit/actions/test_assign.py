# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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

from mergify_engine.actions import assign


def test_assign_get_schema():
    validator = voluptuous.Schema(assign.AssignAction.get_schema())
    schema = {"users": ["{{ author }}"]}
    assert validator(schema) == assign.AssignAction(config=schema)

    schema = {"users": ["foo-42"]}
    assert validator(schema) == assign.AssignAction(config=schema)


def test_assign_get_schema_with_wrong_template():
    validator = voluptuous.Schema(assign.AssignAction.get_schema())

    with pytest.raises(voluptuous.Invalid) as e:
        validator({"users": ["{{ foo }}"]})

    assert str(e.value) == "Template syntax error @ data['users'][0]"
