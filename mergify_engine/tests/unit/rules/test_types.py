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

from mergify_engine.rules import types


@pytest.mark.parametrize(
    "s", ("hello", "{{author}}",),
)
def test_jinja2_valid(s):
    assert types.Jinja2(s) == s


def test_jinja2_invalid():
    with pytest.raises(voluptuous.Invalid) as x:
        types.Jinja2("{{foo")
    assert str(x.value) == "Template syntax error @ data[line 1]"
    assert str(x.value.error_message) == "unexpected end of template, expected 'end of print statement'. at line 1"


def test_jinja2_None():
    with pytest.raises(voluptuous.Invalid) as x:
        types.Jinja2(None)
    assert str(x.value) == "Template cannot be null"
