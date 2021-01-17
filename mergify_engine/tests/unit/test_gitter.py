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
from unittest import mock

import pytest

from mergify_engine import gitter


@pytest.mark.asyncio
async def test_gitter():
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

        with open(git.tmp + "/.mergify.yml", "w") as f:
            f.write("pull_request_rules:")
        await git("add", ".mergify.yml")
        await git("commit", "-m", "Intial commit", "-a", "--no-edit")

        assert os.path.exists(f"{git.tmp}/.git")
    finally:
        await git.cleanup()
        assert not os.path.exists(f"{git.tmp}/.git")
