# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2022 Mergify SAS
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


SERVICE_NAME: str = "engine-<unknown>"
VERSION: str = os.environ.get("MERGIFYENGINE_SHA", "unknown")


def setup(service_name: str, dump_config: bool = True) -> None:
    global SERVICE_NAME
    SERVICE_NAME = "engine-" + service_name

    if config.SENTRY_URL:  # pragma: no cover
        sentry_sdk.init(
            config.SENTRY_URL,
            max_breadcrumbs=10,
            release=VERSION,
            environment=config.SENTRY_ENVIRONMENT,
            integrations=[
                httpx.HttpxIntegration(),
            ],
        )
        sentry_sdk.utils.MAX_STRING_LENGTH = 2048

    ddtrace.config.version = VERSION
    statsd.constant_tags.append(f"service:{SERVICE_NAME}")
    ddtrace.config.service = SERVICE_NAME

    ddtrace.config.httpx["split_by_domain"] = True

    logs.setup_logging(dump_config=dump_config)

    # NOTE(sileht): For security reason, we don't expose env after this point
    # env is authorized during modules loading and pre service initializarion
    # after it's not.
    envs_to_preserve = ("PATH", "LANG", "VIRTUAL_ENV")

    saved_env = {
        env: os.environ[env]
        for env in os.environ
        if env in envs_to_preserve or env.startswith("DD_")
    }
    os.environ.clear()
    os.environ.update(saved_env)
