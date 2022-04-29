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
import subprocess
import sys
from unittest import mock

from mergify_engine import admin


def test_admin_tool_installation(original_environment_variables: None) -> None:
    # FIXME(sileht): this semgrep looks buggy, it think the tuple a dynamic, while it's not.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use.dangerous-subprocess-use
    p = subprocess.run(
        ("mergify-admin", "--help"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert p.returncode == 0
    assert b"usage: mergify-admin [-h]" in p.stdout


@mock.patch.object(sys, "argv", ["mergify-admin", "suspend", "foobar"])
@mock.patch.object(admin, "suspended")
def test_admin_suspend(
    suspended: mock.Mock, original_environment_variables: None
) -> None:
    admin.main()
    assert suspended.mock_calls == [mock.call("PUT", "foobar")]


@mock.patch.object(sys, "argv", ["mergify-admin", "unsuspend", "foobar"])
@mock.patch.object(admin, "suspended")
def test_admin_unsuspend(
    suspended: mock.Mock, original_environment_variables: None
) -> None:
    admin.main()
    assert suspended.mock_calls == [mock.call("DELETE", "foobar")]
