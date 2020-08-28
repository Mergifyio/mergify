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
    "s",
    (
        "hello",
        "{{author}}",
    ),
)
def test_jinja2_valid(s):
    assert types.Jinja2(s) == s


def test_jinja2_invalid():
    with pytest.raises(voluptuous.Invalid) as x:
        types.Jinja2("{{foo")
    assert str(x.value) == "Template syntax error @ data[line 1]"
    assert (
        str(x.value.error_message)
        == "unexpected end of template, expected 'end of print statement'."
    )


def test_jinja2_None():
    with pytest.raises(voluptuous.Invalid) as x:
        types.Jinja2(None)
    assert str(x.value) == "Template cannot be null"


def test_jinja2_unknown_attr():
    with pytest.raises(voluptuous.Invalid) as x:
        types.Jinja2("{{foo}}")
    assert str(x.value) == "Template syntax error"
    assert str(x.value.error_message) == "Unknown pull request attribute: foo"


def test_jinja2_custom_attr():
    s = "{{ role_status }}"
    assert types.Jinja2(s, {"role_status": "passed"}) == s


@pytest.mark.parametrize(
    "login",
    ("foobar", "foobaz", "foo-baz", "f123", "123foo"),
)
def test_github_login_ok(login):
    types.GitHubLogin(login)


@pytest.mark.parametrize(
    "login,error",
    (
        ("-foobar", "GitHub login contains invalid characters"),
        ("foobaz-", "GitHub login contains invalid characters"),
        ("foo-béaz", "GitHub login contains invalid characters"),
        ("🤣", "GitHub login contains invalid characters"),
        ("O_o", "GitHub login contains invalid characters"),
        ("", "A GitHub login cannot be an empty string"),
    ),
)
def test_github_login_nok(login, error):
    with pytest.raises(voluptuous.Invalid) as x:
        types.GitHubLogin(login)
    assert str(x.value) == error


@pytest.mark.parametrize(
    "login,result",
    (
        ("foobar", "foobar"),
        ("foobaz", "foobaz"),
        ("foo-baz", "foo-baz"),
        ("f123", "f123"),
        ("foo/bar", "bar"),
        ("@foo/bar", "bar"),
        ("@fo-o/bar", "bar"),
        ("@fo-o/ba-r", "ba-r"),
        ("@foo/ba-r", "ba-r"),
    ),
)
def test_github_team_ok(login, result):
    assert types.GitHubTeam(login) == result


@pytest.mark.parametrize(
    "login,error",
    (
        ("-foobar", "GitHub team contains invalid characters"),
        ("/-foobar", "A GitHub organization cannot be an empty string"),
        ("foo/-foobar", "GitHub team contains invalid characters"),
        ("foo/-", "GitHub team contains invalid characters"),
        ("foo//-", "GitHub team contains invalid characters"),
        ("/foo//-", "A GitHub organization cannot be an empty string"),
        ("@/foo//-", "A GitHub organization cannot be an empty string"),
        ("@arf/", "A GitHub team cannot be an empty string"),
        ("", "A GitHub team cannot be an empty string"),
    ),
)
def test_github_team_nok(login, error):
    with pytest.raises(voluptuous.Invalid) as x:
        types.GitHubTeam(login)
    assert str(x.value) == error
