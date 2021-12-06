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


from mergify_engine import cache


def test_cache_method() -> None:
    c: cache.Cache[str, bool] = cache.Cache()
    assert c.get("foo") is cache.Unset
    c.set("foo", False)
    assert not c.get("foo")
    c.set("bar", True)
    assert c.get("bar")
    c.delete("foo")
    assert c.get("foo") is cache.Unset
    assert c.get("bar")

    c.clear()
    assert c.get("foo") is cache.Unset
    assert c.get("bar") is cache.Unset


def test_cache_dict_interface() -> None:
    c: cache.Cache[str, bool] = cache.Cache()
    assert c.get("foo") is cache.Unset
    assert c["foo"] is cache.Unset
    c["foo"] = False
    assert not c["foo"]
    c["foo"] = True
    assert c["foo"]
    del c["foo"]
    assert c.get("foo") is cache.Unset
    assert c["foo"] is cache.Unset
    del c["notexist"]


def test_single_cache() -> None:
    c: cache.SingleCache[str] = cache.SingleCache()
    assert c.get() is cache.Unset
    c.set("foo")
    assert c.get() == "foo"
    c.delete()
    assert c.get() is cache.Unset
