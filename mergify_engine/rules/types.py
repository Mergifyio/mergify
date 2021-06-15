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
import dataclasses
import typing

import jinja2.exceptions
import jinja2.sandbox
import voluptuous

from mergify_engine import context
from mergify_engine import github_types


_JINJA_ENV = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)


@dataclasses.dataclass
class LineColumnPath:
    line: int
    column: typing.Optional[int] = None

    def __repr__(self):
        if self.column is None:
            return f"line {self.line}"
        return f"line {self.line}, column {self.column}"


class DummyContext(context.Context):

    # This is only used to check Jinja2 syntax validity and must be sync
    @staticmethod
    def _get_consolidated_data(key):
        if key in context.PullRequest.ATTRIBUTES:
            return None
        elif key in context.PullRequest.LIST_ATTRIBUTES:
            return []
        else:
            raise context.PullRequestAttributeError(key)

    @staticmethod
    def _ensure_complete():
        pass


class DummyPullRequest(context.PullRequest):
    # This is only used to check Jinja2 syntax validity and must be sync
    def __getattr__(self, name: str) -> typing.Any:
        return self.context._get_consolidated_data(name.replace("_", "-"))

    def render_template(self, template: str, extra_variables: typing.Optional[typing.Dict[str, str]] = None) -> str:  # type: ignore[override]
        """Render a template interpolating variables based on pull request attributes."""
        env = jinja2.sandbox.SandboxedEnvironment(
            undefined=jinja2.StrictUndefined,
        )
        with self._template_exceptions_mapping():
            used_variables = jinja2.meta.find_undeclared_variables(env.parse(template))
            infos = {}
            for k in used_variables:
                if extra_variables and k in extra_variables:
                    infos[k] = extra_variables[k]
                else:
                    infos[k] = getattr(self, k)
            return env.from_string(template).render(**infos)


_DUMMY_PR = DummyPullRequest(
    DummyContext(
        None,  # type: ignore
        github_types.GitHubPullRequest(
            {
                "locked": False,
                "assignees": [],
                "requested_reviewers": [],
                "requested_teams": [],
                "milestone": None,
                "title": "",
                "body": "",
                "number": github_types.GitHubPullRequestNumber(0),
                "html_url": "",
                "id": github_types.GitHubPullRequestId(0),
                "maintainer_can_modify": False,
                "state": "open",
                "created_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "updated_at": github_types.ISODateTimeType("2021-06-01T18:41:39Z"),
                "merged": False,
                "merged_by": None,
                "merged_at": None,
                "closed_at": None,
                "draft": False,
                "merge_commit_sha": None,
                "commits": 0,
                "mergeable_state": "unknown",
                "rebaseable": False,
                "changed_files": 1,
                "user": {
                    "id": github_types.GitHubAccountIdType(0),
                    "login": github_types.GitHubLogin(""),
                    "type": "User",
                    "avatar_url": "",
                },
                "labels": [],
                "base": {
                    "user": {
                        "id": github_types.GitHubAccountIdType(0),
                        "login": github_types.GitHubLogin(""),
                        "type": "User",
                        "avatar_url": "",
                    },
                    "label": "",
                    "ref": github_types.GitHubRefType(""),
                    "sha": github_types.SHAType(""),
                    "repo": {
                        "url": "",
                        "html_url": "",
                        "default_branch": github_types.GitHubRefType(""),
                        "full_name": "",
                        "archived": False,
                        "id": github_types.GitHubRepositoryIdType(0),
                        "private": False,
                        "name": github_types.GitHubRepositoryName(""),
                        "owner": {
                            "login": github_types.GitHubLogin(""),
                            "id": github_types.GitHubAccountIdType(0),
                            "type": "User",
                            "avatar_url": "",
                        },
                    },
                },
                "head": {
                    "user": {
                        "id": github_types.GitHubAccountIdType(0),
                        "login": github_types.GitHubLogin(""),
                        "type": "User",
                        "avatar_url": "",
                    },
                    "label": "",
                    "ref": github_types.GitHubRefType(""),
                    "sha": github_types.SHAType(""),
                    "repo": {
                        "url": "",
                        "html_url": "",
                        "default_branch": github_types.GitHubRefType(""),
                        "full_name": "",
                        "archived": False,
                        "id": github_types.GitHubRepositoryIdType(0),
                        "private": False,
                        "name": github_types.GitHubRepositoryName(""),
                        "owner": {
                            "login": github_types.GitHubLogin(""),
                            "id": github_types.GitHubAccountIdType(0),
                            "type": "User",
                            "avatar_url": "",
                        },
                    },
                },
            }
        ),
        [],
    )
)


def Jinja2(
    value: typing.Optional[typing.Any],
    extra_variables: typing.Optional[typing.Dict[str, typing.Any]] = None,
) -> typing.Optional[str]:
    """A Jinja2 type for voluptuous Schemas."""
    if value is None:
        raise voluptuous.Invalid("Template cannot be null")
    if not isinstance(value, str):
        raise voluptuous.Invalid("Template must be a string")
    try:
        # TODO: optimize this by returning, storing and using the parsed Jinja2 AST
        _DUMMY_PR.render_template(value, extra_variables)
    except context.RenderTemplateFailure as rtf:
        if rtf.lineno is None:
            path = None
        else:
            path = [LineColumnPath(rtf.lineno, None)]
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


def _check_GitHubLogin_format(
    value: typing.Optional[str],
    _type: typing.Literal["login", "organization"] = "login",
) -> github_types.GitHubLogin:
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
    return github_types.GitHubLogin(value)


GitHubLogin = voluptuous.All(str, _check_GitHubLogin_format)


@dataclasses.dataclass
class InvalidTeam(Exception):
    details: str


@dataclasses.dataclass(unsafe_hash=True)
class _GitHubTeam:
    team: github_types.GitHubTeamSlug
    organization: typing.Optional[github_types.GitHubLogin]
    raw: str

    @classmethod
    def from_string(cls, value: str) -> "_GitHubTeam":
        if not value:
            raise voluptuous.Invalid("A GitHub team cannot be an empty string")

        # Remove leading @ if any:
        # This format is accepted in conditions so we're happy to accept it here too.
        if value[0] == "@":
            org, sep, team = value[1:].partition("/")
        else:
            org, sep, team = value.partition("/")

        if sep == "" and team == "":
            # Just a slug
            team = org
            final_org = None
        else:
            final_org = _check_GitHubLogin_format(org, "organization")

        if not team:
            raise voluptuous.Invalid("A GitHub team cannot be an empty string")

        if (
            "/" in team
            or team[0] == "-"
            or team[-1] == "-"
            or not team.isascii()
            or not team.replace("-", "").replace("_", "").isalnum()
        ):
            raise voluptuous.Invalid("GitHub team contains invalid characters")

        return cls(github_types.GitHubTeamSlug(team), final_org, value)

    async def has_read_permission(
        self,
        ctxt: context.Context,
    ) -> None:
        expected_organization = ctxt.pull["base"]["repo"]["owner"]["login"]
        if self.organization is not None and self.organization != expected_organization:
            raise InvalidTeam(
                f"Team `{self.raw}` is not part of the organization `{expected_organization}`"
            )

        # TODO(sileht): cache me
        if not await ctxt.repository.team_has_read_permission(self.team):
            raise InvalidTeam(
                f"Team `{self.raw}` does not exist or has not access to this repository"
            )


GitHubTeam = voluptuous.All(str, voluptuous.Coerce(_GitHubTeam.from_string))
