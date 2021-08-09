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
import asyncio

from mergify_engine.clients import github
from mergify_engine.clients import github_app


async def count_seats():
    all_collaborators = set()
    async with github.AsyncGithubInstallationClient(
        auth=github_app.GithubBearerAuth(),
    ) as app_client:
        async for installation in app_client.items("/app/installations"):
            async with github.aget_client(
                installation["account"]["login"], installation["account"]["id"]
            ) as client:
                async for repository in client.items(
                    "/installation/repositories", list_items="repositories"
                ):
                    async for collaborator in client.items(
                        f"{repository['url']}/collaborators"
                    ):
                        if collaborator["permissions"]["push"]:
                            all_collaborators.add(collaborator["login"])

    return f"collaborators: {len(all_collaborators)}"


def main():
    print(asyncio.run(count_seats()))
