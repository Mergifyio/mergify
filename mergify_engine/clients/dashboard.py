# -*- encoding: utf-8 -*-
#
# Copyright © 2022 Mergify SAS
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


from mergify_engine import config
from mergify_engine.clients import http


class AsyncDashboardSaasClient(http.AsyncClient):
    def __init__(self) -> None:
        super().__init__(
            base_url=config.SUBSCRIPTION_BASE_URL,
            headers={"Authorization": f"Bearer {config.ENGINE_TO_DASHBOARD_API_KEY}"},
        )


class AsyncDashboardOnPremiseClient(http.AsyncClient):
    def __init__(self) -> None:
        super().__init__(
            base_url=config.SUBSCRIPTION_BASE_URL,
            headers={"Authorization": f"token {config.SUBSCRIPTION_TOKEN}"},
        )
