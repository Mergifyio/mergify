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
import typing

import jinja2.exceptions
import jinja2.sandbox
import voluptuous

from mergify_engine import rules


_JINJA_ENV = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)


def Jinja2(
    value: str, extra_variables: typing.Optional[typing.Dict[str, typing.Any]] = None
) -> typing.Optional[str]:
    """A Jinja2 type for voluptuous Schemas."""
    if value is None:
        raise voluptuous.Invalid("Template cannot be null")

    # break circular import of context
    from mergify_engine.rules import dummy_context

    try:
        # TODO: optimize this by returning, storing and using the parsed Jinja2 AST
        dummy_context.DUMMY_PR.render_template(value, extra_variables)
    except dummy_context.context.RenderTemplateFailure as rtf:
        if rtf.lineno is None:
            path = None
        else:
            path = [rules.LineColumnPath(rtf.lineno, None)]
        raise voluptuous.Invalid(
            "Template syntax error", error_message=str(rtf), path=path
        )
    return value


def Jinja2WithNone(
    value: str, extra_variables: typing.Optional[typing.Dict[str, typing.Any]] = None
) -> typing.Optional[str]:
    if value is None:
        return None

    return Jinja2(value, extra_variables)


def _check_GitHubLogin_format(value, _type="login"):
    # GitHub says login cannot:
    # - start with an hyphen
    # - ends with an hyphen
    # - contains something else than hyphen and alpha numericals characters
    if not value:
        raise voluptuous.Invalid(f"A GitHub {_type} cannot be an empty string")
    if (
        value[0] == "-"
        or value[-1] == "-"
        or not value.isascii()
        or not value.replace("-", "").isalnum()
    ):
        raise voluptuous.Invalid(f"GitHub {_type} contains invalid characters")
    return value


GitHubLogin = voluptuous.All(str, _check_GitHubLogin_format)


def _check_GitHubTeam_format(value):
    if not value:
        raise voluptuous.Invalid("A GitHub team cannot be an empty string")

    # Remove leading @ if any:
    # This format is accepted in conditions so we're happy to accept it here too.
    if value[0] == "@":
        value = value[1:]

    org, sep, team = value.partition("/")

    if sep == "" and team == "":
        # Just a slug
        team = org
    else:
        _check_GitHubLogin_format(org, "organization")

    if not team:
        raise voluptuous.Invalid("A GitHub team cannot be an empty string")

    if (
        team[0] == "-"
        or team[-1] == "-"
        or not team.isascii()
        or not team.replace("-", "").replace("_", "").isalnum()
    ):
        raise voluptuous.Invalid("GitHub team contains invalid characters")

    return team


GitHubTeam = voluptuous.All(str, _check_GitHubTeam_format)
