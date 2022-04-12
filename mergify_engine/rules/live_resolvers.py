# -*- encoding: utf-8 -*-
#
# Copyright © 2021 Mergify SAS
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
import functools
import itertools
import typing

from mergify_engine import github_types
from mergify_engine.clients import http
from mergify_engine.rules import filter


if typing.TYPE_CHECKING:
    from mergify_engine import context


@dataclasses.dataclass
class LiveResolutionFailure(Exception):
    reason: str


async def _resolve_login(
    repository: "context.Repository", name: str
) -> typing.List[github_types.GitHubLogin]:
    if not name:
        return []
    elif not isinstance(name, str):
        return [github_types.GitHubLogin(name)]
    elif name[0] != "@":
        return [github_types.GitHubLogin(name)]

    if "/" in name:
        organization, _, team_slug = name.partition("/")
        if not team_slug or "/" in team_slug:
            raise LiveResolutionFailure(f"Team `{name}` is invalid")
        organization = github_types.GitHubLogin(organization[1:])
        expected_organization = repository.repo["owner"]["login"]
        if organization != expected_organization:
            raise LiveResolutionFailure(
                f"Team `{name}` is not part of the organization `{expected_organization}`"
            )
        team_slug = github_types.GitHubTeamSlug(team_slug)
    else:
        team_slug = github_types.GitHubTeamSlug(name[1:])

    try:
        return await repository.installation.get_team_members(team_slug)
    except http.HTTPNotFound:
        raise LiveResolutionFailure(f"Team `{name}` does not exist")
    except http.HTTPClientSideError as e:
        repository.log.warning(
            "fail to get the organization, team or members",
            team=name,
            status_code=e.status_code,
            detail=e.message,
        )
        raise LiveResolutionFailure(
            f"Failed retrieve team `{name}`, details: {e.message}"
        )


async def teams(
    repository: "context.Repository",
    values: typing.Optional[typing.Union[typing.List[str], typing.Tuple[str], str]],
) -> typing.List[github_types.GitHubLogin]:

    if not values:
        return []
    # FIXME(sileht): This should not belong here, we should accept only a List[str]
    if not isinstance(values, (list, tuple)):
        values = [values]

    return list(
        itertools.chain.from_iterable(
            [await _resolve_login(repository, value) for value in values]
        )
    )


_TEAM_ATTRIBUTES = (
    "author",
    "merged_by",
    "approved-reviews-by",
    "dismissed-reviews-by",
    "commented-reviews-by",
    "changes-requested-reviews-by",
)


def configure_filter(
    repository: "context.Repository", f: filter.Filter[filter.FilterResultT]
) -> None:
    for attrib in _TEAM_ATTRIBUTES:
        f.value_expanders[attrib] = functools.partial(  # type: ignore[assignment]
            teams, repository
        )
