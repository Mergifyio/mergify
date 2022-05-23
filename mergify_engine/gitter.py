#
# Copyright © 2021–2022 Mergify SAS
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
import os
import re
import shutil
import tempfile
import typing
import urllib.parse

from ddtrace import tracer

from mergify_engine import config
from mergify_engine import github_types
from mergify_engine.dashboard import user_tokens


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


class GitMergifyNamespaceConflict(GitError):
    pass


GIT_MESSAGE_TO_EXCEPTION: typing.Dict[
    typing.Union[str, re.Pattern[str]], typing.Type[GitError]
] = collections.OrderedDict(
    [
        ("Authentication failed", GitAuthenticationFailure),
        ("RPC failed; HTTP 401", GitAuthenticationFailure),
        ("This repository was archived so it is read-only.", GitFatalError),
        ("organization has enabled or enforced SAML SSO.", GitFatalError),
        ("Invalid username or password", GitAuthenticationFailure),
        ("Repository not found", GitAuthenticationFailure),
        ("The requested URL returned error: 403", GitAuthenticationFailure),
        ("remote contains work that you do", GitErrorRetriable),
        ("remote end hung up unexpectedly", GitErrorRetriable),
        (
            re.compile(
                "cannot lock ref 'refs/heads/mergify/[^']*': 'refs/heads/mergify(|/bp|/copy|/merge-queue)' exists"
            ),
            GitMergifyNamespaceConflict,
        ),
        ("cannot lock ref 'refs/heads/", GitErrorRetriable),
        ("Could not resolve host", GitErrorRetriable),
        ("Operation timed out", GitErrorRetriable),
        ("Connection timed out", GitErrorRetriable),
        ("No such device or address", GitErrorRetriable),
        ("Protected branch update failed", GitFatalError),
        ("couldn't find remote ref", GitFatalError),
    ]
)


@dataclasses.dataclass
class Gitter(object):
    logger: "logging.LoggerAdapter[logging.Logger]"
    tmp: typing.Optional[str] = None

    # Worker timeout at 5 minutes, so ensure subprocess return before
    GIT_COMMAND_TIMEOUT: int = dataclasses.field(init=False, default=4 * 60 + 30)

    @tracer.wrap("gitter.init", span_type="worker")
    async def init(self) -> None:
        # TODO(sileht): use aiofiles instead of thread
        self.tmp = await asyncio.to_thread(  # type: ignore[call-arg]
            tempfile.mkdtemp, prefix="mergify-gitter"
        )
        if self.tmp is None:
            raise RuntimeError("mkdtemp failed")
        self.repository = os.path.join(self.tmp, "repository")
        # TODO(sileht): use aiofiles instead of thread
        await asyncio.to_thread(os.mkdir, self.repository)

        self.env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NOGLOB_PATHSPECS": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_ALLOW_PROTOCOL": "https",
            "PATH": os.environ["PATH"],
            "HOME": self.tmp,
            "TMPDIR": self.tmp,
        }
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
        # Use one git cache daemon per Gitter
        await self("config", "credential.useHttpPath", "true")
        await self(
            "config",
            "credential.helper",
            f"cache --timeout=300 --socket={self.tmp}/.git-creds-socket",
        )

    def prepare_safe_env(
        self,
        _env: typing.Optional[typing.Dict[str, str]] = None,
    ) -> typing.Dict[str, str]:
        safe_env = self.env.copy()
        if _env is not None:
            safe_env.update(_env)
        return safe_env

    async def __call__(
        self,
        *args: str,
        _input: typing.Optional[str] = None,
        _env: typing.Optional[typing.Dict[str, str]] = None,
    ) -> str:
        if self.repository is None:
            raise RuntimeError("__call__() called before init()")

        self.logger.info("calling: %s", " ".join(args))

        with tracer.trace("gitter.call", span_type="worker") as span:
            span.set_tag("args", " ".join(args))
            try:
                # TODO(sileht): Current user provided data in git commands are safe, but we should create an
                # helper function for each git command to double check the input is
                # safe, eg: like a second seatbelt. See: MRGFY-930
                # nosemgrep: python.lang.security.audit.dangerous-asyncio-create-exec.dangerous-asyncio-create-exec
                p = await asyncio.create_subprocess_exec(
                    "git",
                    *args,
                    cwd=self.repository,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    stdin=None if _input is None else asyncio.subprocess.PIPE,
                    env=self.prepare_safe_env(_env),
                )

                stdout, _ = await asyncio.wait_for(
                    p.communicate(
                        input=None if _input is None else _input.encode("utf8")
                    ),
                    self.GIT_COMMAND_TIMEOUT,
                )
                output = stdout.decode("utf-8")
                if p.returncode:
                    raise self._get_git_exception(p.returncode, output)
                else:
                    return output
            finally:
                self.logger.debug("finish: %s", " ".join(args))

    @classmethod
    def _get_git_exception(cls, returncode: int, output: str) -> GitError:
        if output == "":
            # SIGKILL...
            return GitErrorRetriable(returncode, "Git process got killed")
        elif cls._is_force_push_lease_reject(output):
            return GitErrorRetriable(
                returncode,
                "Remote branch changed in the meantime: \n" f"```\n{output}\n```\n",
            )

        for pattern, out_exception in GIT_MESSAGE_TO_EXCEPTION.items():
            if isinstance(pattern, re.Pattern):
                match = pattern.search(output) is not None
            else:
                match = pattern in output
            if match:
                return out_exception(
                    returncode,
                    output,
                )

        return GitError(returncode, output)

    @staticmethod
    def _is_force_push_lease_reject(message: str) -> bool:
        return (
            "failed to push some refs" in message
            and "[rejected]" in message
            and "(stale info)" in message
        )

    @tracer.wrap("gitter.cleanup", span_type="worker")
    async def cleanup(self) -> None:
        if self.tmp is None:
            return

        self.logger.info("cleaning: %s", self.tmp)
        try:
            await self(
                "credential-cache",
                f"--socket={self.tmp}/.git-creds-socket",
                "exit",
            )
        except GitError:  # pragma: no cover
            self.logger.warning("git credential-cache exit fail")
        # TODO(sileht): use aiofiles instead of thread
        await asyncio.to_thread(shutil.rmtree, self.tmp)

    async def configure(
        self, user: typing.Optional[user_tokens.UserTokensUser] = None
    ) -> None:
        if user is None:
            name = "Mergify"
            login = config.BOT_USER_LOGIN
            account_id = config.BOT_USER_ID
        else:
            name = user["name"] or user["login"]
            login = user["login"]
            account_id = user["id"]

        await self("config", "user.name", name)
        await self(
            "config",
            "user.email",
            f"{account_id}+{login}@users.noreply.{config.GITHUB_DOMAIN}",
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
