# -*- encoding: utf-8 -*-
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

import sentry_sdk
from sentry_sdk.integrations.redis import RedisIntegration

from mergify_engine import config
from mergify_engine import logs


LOG = logs.getLogger(__name__)


if config.SENTRY_URL:  # pragma: no cover
    sentry_sdk.init(
        config.SENTRY_URL,
        max_breadcrumbs=10,
        release=os.environ.get("HEROKU_RELEASE_VERSION"),
        environment=config.SENTRY_ENVIRONMENT,
        integrations=[RedisIntegration()],
    )
