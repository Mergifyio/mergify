#
# Copyright © 2019–2020 Mergify SAS
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

from mergify_engine import utils


def test_unicode_truncate():
    s = "hé ho! how are you√2?"
    assert utils.unicode_truncate(s, 0) == ""
    assert utils.unicode_truncate(s, 1) == "h"
    assert utils.unicode_truncate(s, 2) == "h"
    assert utils.unicode_truncate(s, 3) == "hé"
    assert utils.unicode_truncate(s, 4) == "hé "
    assert utils.unicode_truncate(s, 10) == "hé ho! ho"
    assert utils.unicode_truncate(s, 18) == "hé ho! how are yo"
    assert utils.unicode_truncate(s, 19) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 20) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 21) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 22) == "hé ho! how are you√"
    assert utils.unicode_truncate(s, 23) == "hé ho! how are you√2"
    assert utils.unicode_truncate(s, 50) == s


def test_process_identifier():
    assert isinstance(utils._PROCESS_IDENTIFIER, str)


def test_get_random_choices():
    choices = {
        "jd": 10,
        "sileht": 1,
        "foobar": 3,
    }
    assert utils.get_random_choices(0, choices, 1) == {"foobar"}
    assert utils.get_random_choices(1, choices, 1) == {"foobar"}
    assert utils.get_random_choices(2, choices, 1) == {"foobar"}
    assert utils.get_random_choices(3, choices, 1) == {"jd"}
    assert utils.get_random_choices(4, choices, 1) == {"jd"}
    assert utils.get_random_choices(11, choices, 1) == {"jd"}
    assert utils.get_random_choices(12, choices, 1) == {"jd"}
    assert utils.get_random_choices(13, choices, 1) == {"sileht"}
    assert utils.get_random_choices(14, choices, 1) == {"foobar"}
    assert utils.get_random_choices(15, choices, 1) == {"foobar"}
    assert utils.get_random_choices(16, choices, 1) == {"foobar"}
    assert utils.get_random_choices(17, choices, 1) == {"jd"}
    assert utils.get_random_choices(18, choices, 1) == {"jd"}
    assert utils.get_random_choices(19, choices, 1) == {"jd"}
    assert utils.get_random_choices(20, choices, 1) == {"jd"}
    assert utils.get_random_choices(21, choices, 1) == {"jd"}
    assert utils.get_random_choices(22, choices, 1) == {"jd"}
    assert utils.get_random_choices(23, choices, 1) == {"jd"}
    assert utils.get_random_choices(24, choices, 1) == {"jd"}
    assert utils.get_random_choices(25, choices, 1) == {"jd"}
    assert utils.get_random_choices(26, choices, 1) == {"jd"}
    assert utils.get_random_choices(27, choices, 1) == {"sileht"}
    assert utils.get_random_choices(28, choices, 1) == {"foobar"}
    assert utils.get_random_choices(29, choices, 1) == {"foobar"}
    assert utils.get_random_choices(30, choices, 1) == {"foobar"}
    assert utils.get_random_choices(31, choices, 1) == {"jd"}
    assert utils.get_random_choices(32, choices, 1) == {"jd"}
    assert utils.get_random_choices(23, choices, 2) == {"sileht", "jd"}
    assert utils.get_random_choices(2, choices, 2) == {"jd", "foobar"}
    assert utils.get_random_choices(4, choices, 2) == {"jd", "foobar"}
    assert utils.get_random_choices(0, choices, 3) == {"jd", "sileht", "foobar"}
    with pytest.raises(ValueError):
        assert utils.get_random_choices(4, choices, 4) == {"jd", "sileht"}
