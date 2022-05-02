#
# Copyright © 2019–2020 Mergify SAS
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
import typing

import msgpack
import pytest

from mergify_engine import github_types
from mergify_engine import redis_utils
from mergify_engine import utils


@pytest.mark.parametrize(
    "length,placeholder,expected",
    (
        (0, "", ""),
        (1, "", "h"),
        (2, "", "h"),
        (3, "", "hé"),
        (4, "", "hé "),
        (10, "", "hé ho! ho"),
        (18, "", "hé ho! how are yo"),
        (19, "", "hé ho! how are you"),
        (20, "", "hé ho! how are you"),
        (21, "", "hé ho! how are you"),
        (22, "", "hé ho! how are you√"),
        (23, "", "hé ho! how are you√2"),
        (50, "", "hé ho! how are you√2?"),
        # ellipsis
        (0, "…", None),
        (1, "…", None),
        (2, "…", None),
        (3, "…", "…"),
        (4, "…", "h…"),
        (5, "…", "h…"),
        (6, "…", "hé…"),
        (7, "…", "hé …"),
        (13, "…", "hé ho! ho…"),
        (21, "…", "hé ho! how are yo…"),
        (22, "…", "hé ho! how are you…"),
        (23, "…", "hé ho! how are you…"),
        (24, "…", "hé ho! how are you√2?"),
        (50, "…", "hé ho! how are you√2?"),
        (21, "😎", "hé ho! how are y😎"),
        (22, "😎", "hé ho! how are yo😎"),
        (23, "😎", "hé ho! how are you😎"),
        (24, "😎", "hé ho! how are you√2?"),
        (50, "😎", "hé ho! how are you√2?"),
        (3, "😎", None),
        (4, "😎", "😎"),
        (5, "😎", "h😎"),
        (6, "😎", "h😎"),
        (7, "😎", "hé😎"),
    ),
)
def test_unicode_truncate(
    length: int,
    placeholder: str,
    expected: typing.Optional[str],
) -> None:
    s = "hé ho! how are you√2?"
    if expected is None:
        with pytest.raises(ValueError):
            utils.unicode_truncate(s, length, placeholder)
    else:
        result = utils.unicode_truncate(s, length, placeholder)
        assert len(result.encode()) <= length
        assert result == expected


def test_process_identifier() -> None:
    assert isinstance(utils._PROCESS_IDENTIFIER, str)


def test_get_random_choices() -> None:
    choices = {
        "jd": 10,
        "sileht": 1,
        "foobar": 3,
    }
    assert utils.get_random_choices(0, choices, 1) == {"foobar"}
    assert utils.get_random_choices(1, choices, 1) == {"foobar"}
    assert utils.get_random_choices(2, choices, 1) == {"foobar"}
    assert utils.get_random_choices(3, choices, 1) == {"jd"}
    assert utils.get_random_choices(4, choices, 1) == {"jd"}
    assert utils.get_random_choices(11, choices, 1) == {"jd"}
    assert utils.get_random_choices(12, choices, 1) == {"jd"}
    assert utils.get_random_choices(13, choices, 1) == {"sileht"}
    assert utils.get_random_choices(14, choices, 1) == {"foobar"}
    assert utils.get_random_choices(15, choices, 1) == {"foobar"}
    assert utils.get_random_choices(16, choices, 1) == {"foobar"}
    assert utils.get_random_choices(17, choices, 1) == {"jd"}
    assert utils.get_random_choices(18, choices, 1) == {"jd"}
    assert utils.get_random_choices(19, choices, 1) == {"jd"}
    assert utils.get_random_choices(20, choices, 1) == {"jd"}
    assert utils.get_random_choices(21, choices, 1) == {"jd"}
    assert utils.get_random_choices(22, choices, 1) == {"jd"}
    assert utils.get_random_choices(23, choices, 1) == {"jd"}
    assert utils.get_random_choices(24, choices, 1) == {"jd"}
    assert utils.get_random_choices(25, choices, 1) == {"jd"}
    assert utils.get_random_choices(26, choices, 1) == {"jd"}
    assert utils.get_random_choices(27, choices, 1) == {"sileht"}
    assert utils.get_random_choices(28, choices, 1) == {"foobar"}
    assert utils.get_random_choices(29, choices, 1) == {"foobar"}
    assert utils.get_random_choices(30, choices, 1) == {"foobar"}
    assert utils.get_random_choices(31, choices, 1) == {"jd"}
    assert utils.get_random_choices(32, choices, 1) == {"jd"}
    assert utils.get_random_choices(23, choices, 2) == {"sileht", "jd"}
    assert utils.get_random_choices(2, choices, 2) == {"jd", "foobar"}
    assert utils.get_random_choices(4, choices, 2) == {"jd", "foobar"}
    assert utils.get_random_choices(0, choices, 3) == {"jd", "sileht", "foobar"}
    with pytest.raises(ValueError):
        assert utils.get_random_choices(4, choices, 4) == {"jd", "sileht"}


def test_to_ordinal_numeric():
    with pytest.raises(ValueError):
        utils.to_ordinal_numeric(-1)

    assert utils.to_ordinal_numeric(0) == "0th"
    assert utils.to_ordinal_numeric(100) == "100th"
    assert utils.to_ordinal_numeric(1) == "1st"
    assert utils.to_ordinal_numeric(11) == "11th"
    assert utils.to_ordinal_numeric(12) == "12th"
    assert utils.to_ordinal_numeric(13) == "13th"
    assert utils.to_ordinal_numeric(2) == "2nd"
    assert utils.to_ordinal_numeric(111) == "111th"
    assert utils.to_ordinal_numeric(112) == "112th"
    assert utils.to_ordinal_numeric(113) == "113th"
    assert utils.to_ordinal_numeric(42) == "42nd"
    assert utils.to_ordinal_numeric(6543512) == "6543512th"
    assert utils.to_ordinal_numeric(6543522) == "6543522nd"
    assert utils.to_ordinal_numeric(3) == "3rd"
    assert utils.to_ordinal_numeric(5743) == "5743rd"
    for i in range(4, 10):
        assert utils.to_ordinal_numeric(i) == f"{i}th"

    assert utils.to_ordinal_numeric(4567) == "4567th"
    assert utils.to_ordinal_numeric(5743) == "5743rd"


def test_split_list():
    assert list(utils.split_list([1, 2, 3, 4, 5, 6, 7, 8, 9], 2)) == [
        [1, 2, 3, 4, 5],
        [6, 7, 8, 9],
    ]
    assert list(utils.split_list([1, 2, 3, 4, 5, 6, 7, 8, 9], 3)) == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    assert list(utils.split_list([1, 2, 3, 4, 5, 6, 7, 8, 9], 4)) == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    assert list(utils.split_list([1, 2], 4)) == [
        [1],
        [2],
    ]
    assert list(utils.split_list([1, 2], 2)) == [
        [1],
        [2],
    ]
    assert list(utils.split_list([1], 2)) == [
        [1],
    ]


async def test_refresh_with_pull_request_number(
    redis_stream: redis_utils.RedisStream,
) -> None:

    gh_owner = github_types.GitHubAccount(
        {
            "id": github_types.GitHubAccountIdType(1),
            "login": github_types.GitHubLogin("foo"),
            "type": "User",
            "avatar_url": "",
        }
    )

    gh_repo = github_types.GitHubRepository(
        {
            "id": github_types.GitHubRepositoryIdType(0),
            "owner": gh_owner,
            "full_name": "",
            "archived": False,
            "url": "",
            "html_url": "",
            "default_branch": github_types.GitHubRefType(""),
            "name": github_types.GitHubRepositoryName("test"),
            "private": False,
        }
    )

    await utils.send_pull_refresh(
        redis_stream,
        gh_repo,
        pull_request_number=github_types.GitHubPullRequestNumber(5),
        action="internal",
        source="test",
    )
    await utils.send_branch_refresh(
        redis_stream,
        gh_repo,
        ref=github_types.GitHubRefType("master"),
        action="admin",
        source="test",
    )

    keys = await redis_stream.keys("*")
    assert set(keys) == {
        b"bucket~1",
        b"bucket-sources~0~0",
        b"bucket-sources~0~5",
        b"streams",
    }

    messages = await redis_stream.xrange("bucket-sources~0~5")
    assert len(messages) == 1
    event = msgpack.unpackb(messages[0][1][b"source"])["data"]
    assert event["action"] == "internal"
    assert event["ref"] is None
    assert event["pull_request_number"] == 5

    messages = await redis_stream.xrange("bucket-sources~0~0")
    assert len(messages) == 1
    event = msgpack.unpackb(messages[0][1][b"source"])["data"]
    assert event["action"] == "admin"
    assert event["ref"] == "master"
    assert event["pull_request_number"] is None
