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


import httpx
import pytest

from mergify_engine import config
from mergify_engine.tests.functional import conftest as func_conftest


@pytest.mark.recorder
async def test_api_application(
    mergify_web_client: httpx.AsyncClient,
    dashboard: func_conftest.DashboardFixture,
) -> None:
    r = await mergify_web_client.get(
        "/v1/application",
        headers={"Authorization": f"bearer {dashboard.api_key_admin}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": 123,
        "name": "testing application",
        "account_scope": {
            "id": config.TESTING_ORGANIZATION_ID,
            "login": config.TESTING_ORGANIZATION_NAME,
        },
    }
