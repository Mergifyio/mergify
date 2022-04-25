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

import os
import typing
from unittest import mock

import pytest

from mergify_engine import gitter


async def test_gitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    git = gitter.Gitter(mock.Mock())
    try:
        await git.init()
        await git.configure()
        await git.add_cred("foo", "bar", "https://github.com")

        with pytest.raises(gitter.GitError) as exc_info:
            await git("add", "toto")
        assert exc_info.value.returncode == 128
        assert (
            exc_info.value.output == "fatal: pathspec 'toto' did not match any files\n"
        )

        if git.repository is None:
            pytest.fail("No tmp dir")

        with open(git.repository + "/.mergify.yml", "w") as f:
            f.write("pull_request_rules:")
        await git("add", ".mergify.yml")
        await git("commit", "-m", "Intial commit", "-a", "--no-edit")

        assert os.path.exists(f"{git.repository}/.git")
    finally:
        await git.cleanup()
        assert not os.path.exists(f"{git.repository}/.git")


@pytest.mark.parametrize(
    "stdout, exception, exc_message",
    (
        (
            """To https://github.com/owner/repo
 ! [remote rejected] mergify/bp/main/pr-42 -> mergify/bp/main/pr-42 (cannot lock ref 'refs/heads/mergify/bp/main/pr-42': 'refs/heads/mergify/merge-queue' exists; cannot create 'refs/heads/mergify/bp/main/pr-42')
error: failed to push some refs to 'https://github.com/owner/repo'

""",
            gitter.GitMergifyNamespaceConflict,
            "cannot lock ref",
        ),
        (
            """To https://github.com/owner/repo
 ! [remote rejected] mergify/bp/main/pr-42 -> mergify/bp/main/pr-42 (cannot lock ref 'refs/heads/mergify/bp/main/pr-42': 'refs/heads/mergify' exists; cannot create 'refs/heads/mergify/bp/main/pr-42')
error: failed to push some refs to 'https://github.com/owner/repo'

""",
            gitter.GitMergifyNamespaceConflict,
            "cannot lock ref",
        ),
        (
            """
To gitlab.com:example/my-project.git
 ! [rejected]        my-branch -> my-branch (stale info)
error: failed to push some refs to 'git@gitlab.com:example/my-project.git'
""",
            gitter.GitErrorRetriable,
            "Remote branch changed in the meantime",
        ),
    ),
)
def test_gitter_error_catching(
    stdout: str, exception: typing.Type[Exception], exc_message: str
) -> None:
    with pytest.raises(exception) as excinfo:
        raise gitter.Gitter._get_git_exception(1, stdout)
    assert exc_message in str(excinfo.value)
