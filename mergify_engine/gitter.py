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
import dataclasses
import logging
import shutil
import tempfile
import typing
import urllib.parse

from mergify_engine import config


@dataclasses.dataclass
class GitError(Exception):
    returncode: int
    output: str


@dataclasses.dataclass
class Gitter(object):
    logger: logging.LoggerAdapter
    tmp: str = dataclasses.field(init=False)

    # Worker timeout at 5 minutes, so ensure subprocess return before
    GIT_COMMAND_TIMEOUT: int = dataclasses.field(init=False, default=4 * 60 + 30)

    async def init(self) -> None:
        self.tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="mergify-gitter")
        self.logger.info("working in: %s", self.tmp)
        await self("init")

    async def __call__(self, *args: str, _input: typing.Optional[str] = None) -> str:
        self.logger.info("calling: %s", " ".join(args))
        try:
            p = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=self.tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=None if _input is None else asyncio.subprocess.PIPE,
            )

            stdout, _ = await asyncio.wait_for(
                p.communicate(input=None if _input is None else _input.encode("utf8")),
                self.GIT_COMMAND_TIMEOUT,
            )
            output = stdout.decode("utf-8")
            if p.returncode:
                raise GitError(p.returncode, output)
            else:
                return output
        finally:
            self.logger.debug("finish: %s", " ".join(args))

    async def cleanup(self) -> None:
        self.logger.info("cleaning: %s", self.tmp)
        try:
            await self(
                "credential-cache", f"--socket={self.tmp}/.git/creds/socket", "exit"
            )
        except GitError:  # pragma: no cover
            self.logger.warning("git credential-cache exit fail")
        await asyncio.to_thread(shutil.rmtree, self.tmp)

    async def configure(self) -> None:
        await self("config", "user.name", f"{config.CONTEXT}-bot")
        await self("config", "user.email", config.GIT_EMAIL)
        # Use one git cache daemon per Gitter
        await self("config", "credential.useHttpPath", "true")
        await self(
            "config",
            "credential.helper",
            f"cache --timeout=300 --socket={self.tmp}/.git/creds/socket",
        )

    async def add_cred(self, username: str, password: str, path: str) -> None:
        parsed = list(urllib.parse.urlparse(config.GITHUB_URL))
        parsed[1] = f"{username}:{password}@{parsed[1]}"
        parsed[2] = path
        url = urllib.parse.urlunparse(parsed)
        await self("credential", "approve", _input=f"url={url}\n\n")
