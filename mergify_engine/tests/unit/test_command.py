# -*- encoding: utf-8 -*-
#
# Copyright © 2019 Mehdi Abaakouk <sileht@sileht.net>
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


from mergify_engine.actions.backport import BackportAction
from mergify_engine.actions.rebase import RebaseAction
from mergify_engine.engine.commands_runner import load_action


def test_command_loader():
    action = load_action("@mergifyio notexist foobar\n")
    assert action is None

    action = load_action("@mergifyio comment foobar\n")
    assert action is None

    action = load_action("@Mergifyio comment foobar\n")
    assert action is None

    for message in [
        "@mergify rebase",
        "@mergifyio rebase",
        "@Mergifyio rebase",
        "@mergifyio rebase\n",
        "@mergifyio rebase foobar",
        "@mergifyio rebase foobar\nsecondline\n",
    ]:
        command, args, action = load_action(message)
        assert command == "rebase"
        assert isinstance(action, RebaseAction)

    command, args, action = load_action(
        "@mergifyio backport branch-3.1 branch-3.2\nfoobar\n"
    )
    assert command == "backport"
    assert args == "branch-3.1 branch-3.2"
    assert isinstance(action, BackportAction)
    assert action.config == {
        "branches": ["branch-3.1", "branch-3.2"],
        "regexes": [],
        "ignore_conflicts": True,
        "label_conflicts": "conflicts",
    }
