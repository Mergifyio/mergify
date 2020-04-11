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
from mergify_engine import context


def test_review_permission_cache():
    class FakeClient(object):
        def __init__(self, repo):
            self.repo = repo

        def item(self, url, *args, **kwargs):
            if self.repo == 1:
                if url == "collaborators/foo/permission":
                    return {"permission": "admin"}
                elif url.startswith("collaborators/"):
                    return {"permission": "loser"}
            elif self.repo == 2:
                if url == "collaborators/bar/permission":
                    return {"permission": "admin"}
                elif url.startswith("collaborators/"):
                    return {"permission": "loser"}
            raise ValueError(f"Unknown test URL `{url}` for repo {self.repo}")

    pr = {
        "number": 123,
        "state": "closed",
        "mergeable_state": "hello",
        "merged_by": None,
        "merged": None,
        "merged_at": None,
    }

    c = context.Context(FakeClient(1), pr,)
    assert c._write_permission_cache.currsize == 0
    assert c.has_write_permissions("foo")
    assert c._write_permission_cache.currsize == 1
    assert c.has_write_permissions("foo")
    assert c._write_permission_cache.currsize == 1
    assert not c.has_write_permissions("bar")
    assert c._write_permission_cache.currsize == 2
    assert not c.has_write_permissions("bar")
    assert c._write_permission_cache.currsize == 2
    assert not c.has_write_permissions("baz")
    assert c._write_permission_cache.currsize == 3
    assert not c.has_write_permissions("baz")
    assert c._write_permission_cache.currsize == 3
    c = context.Context(FakeClient(2), pr,)
    assert c._write_permission_cache.currsize == 0
    assert c.has_write_permissions("bar")
    assert c._write_permission_cache.currsize == 1
    assert c.has_write_permissions("bar")
    assert c._write_permission_cache.currsize == 1
    assert not c.has_write_permissions("foo")
    assert c._write_permission_cache.currsize == 2
    assert not c.has_write_permissions("foo")
    assert c._write_permission_cache.currsize == 2
    assert not c.has_write_permissions("baz")
    assert c._write_permission_cache.currsize == 3
    assert not c.has_write_permissions("baz")
    assert c._write_permission_cache.currsize == 3
