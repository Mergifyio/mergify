# -*- encoding: utf-8 -*-
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
import argparse
import asyncio
import typing

from mergify_engine import github_types
from mergify_engine.clients import github
from mergify_engine.clients import github_app


async def suspended(
    verb: typing.Literal["PUT", "DELETE"], owner: github_types.GitHubLogin
) -> None:
    async with github.AsyncGithubClient(auth=github_app.GithubBearerAuth()) as client:
        installation = typing.cast(
            github_types.GitHubInstallation,
            await client.item(f"/orgs/{owner}/installation"),
        )
        resp = await client.request(
            verb, f"/app/installations/{installation['id']}/suspended"
        )
        print(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mergify admin tools")
    subparsers = parser.add_subparsers(dest="command")
    suspend_parser = subparsers.add_parser("suspend", help="Suspend an installation")
    suspend_parser.add_argument("organization", help="Organization login")
    unsuspend_parser = subparsers.add_parser(
        "unsuspend", help="Unsuspend an installation"
    )
    unsuspend_parser.add_argument("organization", help="Organization login")

    args = parser.parse_args()

    try:
        if args.command == "suspend":
            asyncio.run(suspended("PUT", github_types.GitHubLogin(args.organization)))
        elif args.command == "unsuspend":
            asyncio.run(
                suspended("DELETE", github_types.GitHubLogin(args.organization))
            )
        else:
            parser.print_help()
    except KeyboardInterrupt:
        print("Interruped...")
    except BrokenPipeError:
        pass
