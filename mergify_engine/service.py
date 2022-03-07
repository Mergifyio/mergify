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

from datadog import statsd
import ddtrace
import sentry_sdk
from sentry_sdk.integrations import httpx

from mergify_engine import config
from mergify_engine import logs


def setup(service_name: str, dump_config: bool = True) -> None:
    service_name = "engine-" + service_name

    _version = os.environ.get("HEROKU_RELEASE_VERSION")

    if config.SENTRY_URL:  # pragma: no cover
        sentry_sdk.init(
            config.SENTRY_URL,
            max_breadcrumbs=10,
            release=_version,
            environment=config.SENTRY_ENVIRONMENT,
            integrations=[
                httpx.HttpxIntegration(),
            ],
        )
        sentry_sdk.utils.MAX_STRING_LENGTH = 2048

    ddtrace.config.version = _version
    statsd.constant_tags.append(f"service:{service_name}")
    ddtrace.config.service = service_name

    ddtrace.config.httpx["split_by_domain"] = True

    logs.setup_logging(dump_config=dump_config)
