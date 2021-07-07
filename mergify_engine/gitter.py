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
import collections
import dataclasses
import logging
import shutil
import tempfile
import typing
import urllib.parse

from mergify_engine import config
from mergify_engine import github_types


@dataclasses.dataclass
class GitError(Exception):
    returncode: int
    output: str


class GitFatalError(GitError):
    pass


class GitErrorRetriable(GitError):
    pass


class GitAuthenticationFailure(GitError):
    pass


class GitReferenceAlreadyExists(GitError):
    pass


GIT_MESSAGE_TO_EXCEPTION = collections.OrderedDict(
    [
        ("Authentication failed", GitAuthenticationFailure),
        ("This repository was archived so it is read-only.", GitFatalError),
        ("organization has enabled or enforced SAML SSO.", GitFatalError),
        ("could not apply", GitFatalError),
        ("Invalid username or password", GitAuthenticationFailure),
        ("Repository not found", GitAuthenticationFailure),
        ("The requested URL returned error: 403", GitAuthenticationFailure),
        ("Patch failed at", GitFatalError),
        ("remote contains work that you do", GitErrorRetriable),
        ("remote end hung up unexpectedly", GitErrorRetriable),
        ("cannot lock ref 'refs/heads/", GitErrorRetriable),
        ("Could not resolve host", GitErrorRetriable),
        ("Operation timed out", GitErrorRetriable),
        ("No such device or address", GitErrorRetriable),
        ("Protected branch update failed", GitFatalError),
        ("couldn't find remote ref", GitFatalError),
    ]
)


@dataclasses.dataclass
class Gitter(object):
    logger: logging.LoggerAdapter
    tmp: typing.Optional[str] = None

    # Worker timeout at 5 minutes, so ensure subprocess return before
    GIT_COMMAND_TIMEOUT: int = dataclasses.field(init=False, default=4 * 60 + 30)

    async def init(self) -> None:
        self.tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="mergify-gitter")
        version = await self("version")
        self.logger.info("git directory created", path=self.tmp, version=version)
        await self("init", "--initial-branch=tmp-mergify-trunk")
        # NOTE(sileht): Bump the repository format. This ensures required
        # extensions (promisor, partialclonefilter) are present in git cli and
        # raise an error if not. Avoiding git cli to fallback to full clone
        # behavior for us.
        await self("config", "core.repositoryformatversion", "1")
        # Disable gc since this is a thrown-away repository
        await self("config", "gc.auto", "0")

    async def __call__(self, *args: str, _input: typing.Optional[str] = None) -> str:
        if self.tmp is None:
            raise RuntimeError("__call__() called before init()")

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
                if output == "":
                    # SIGKILL...
                    raise GitErrorRetriable(p.returncode, "Git process got killed")
                elif self._is_force_push_lease_reject(output):
                    raise GitErrorRetriable(
                        p.returncode,
                        "Remote branch changed in the meantime: \n"
                        f"```\n{output}\n```\n",
                    )

                for message, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
                    if message in output:
                        raise out_exception(
                            p.returncode,
                            output,
                        )

                raise GitError(p.returncode, output)
            else:
                return output
        finally:
            self.logger.debug("finish: %s", " ".join(args))

    @staticmethod
    def _is_force_push_lease_reject(message: str) -> bool:
        return (
            "failed to push some refs" in message
            and "[rejected]" in message
            and "(stale info)" in message
        )

    async def cleanup(self) -> None:
        if self.tmp is None:
            return

        self.logger.info("cleaning: %s", self.tmp)
        try:
            await self(
                "credential-cache", f"--socket={self.tmp}/.git/creds/socket", "exit"
            )
        except GitError:  # pragma: no cover
            self.logger.warning("git credential-cache exit fail")
        await asyncio.to_thread(shutil.rmtree, self.tmp)

    async def configure(
        self, name: typing.Optional[str] = None, email: typing.Optional[str] = None
    ) -> None:
        if name is None:
            name = f"{config.CONTEXT}-bot"
        if email is None:
            email = config.GIT_EMAIL
        await self("config", "user.name", name)
        await self("config", "user.email", email)
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

    async def setup_remote(
        self,
        name: str,
        repository: github_types.GitHubRepository,
        username: str,
        password: str,
    ) -> None:
        await self.add_cred(username, password, repository["full_name"])
        await self(
            "remote", "add", name, f"{config.GITHUB_URL}/{repository['full_name']}"
        )
        await self("config", f"remote.{name}.promisor", "true")
        await self("config", f"remote.{name}.partialclonefilter", "blob:none")
