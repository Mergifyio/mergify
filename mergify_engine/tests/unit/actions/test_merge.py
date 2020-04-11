# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
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
from mergify_engine.actions.merge import action


def test_merge_commit_message():
    assert (
        action.MergeAction._get_commit_message(
            {
                "body": """Hello world

# Commit Message
my title

my body""",
            }
        )
        == ("my title", "my body")
    )
    assert (
        action.MergeAction._get_commit_message(
            {
                "body": """Hello world

# Commit Message:
my title

my body
is longer""",
            }
        )
        == ("my title", "my body\nis longer")
    )
    assert (
        action.MergeAction._get_commit_message(
            {
                "body": """Hello world

# Commit Message

my title

my body"""
            }
        )
        == ("my title", "my body")
    )
    assert (
        action.MergeAction._get_commit_message(
            {
                "body": """Hello world

# Commit Message

my title
WATCHOUT ^^^ there is empty spaces above for testing ^^^^
my body"""  # noqa:W293
            },
        )
        == (
            "my title",
            "WATCHOUT ^^^ there is empty spaces above for testing ^^^^\nmy body",
        )
    )
    assert action.MergeAction._get_commit_message(
        {"title": "Fix something", "body": "Here's my message",}, mode="title+body",
    ) == ("Fix something", "Here's my message",)
