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


import contextvars
import logging
import os
import re
import sys

import daiquiri
import daiquiri.formatter
import ddtrace

from mergify_engine import config


LOG = daiquiri.getLogger(__name__)


logging.addLevelName(42, "TEST")
LEVEL_COLORS = daiquiri.formatter.ColorFormatter.LEVEL_COLORS.copy()
LEVEL_COLORS[42] = "\033[01;35m"


WORKER_ID: contextvars.ContextVar[str] = contextvars.ContextVar("worker_id")


class CustomFormatter(daiquiri.formatter.ColorExtrasFormatter):  # type: ignore[misc]
    LEVEL_COLORS = LEVEL_COLORS

    def format(self, record):
        if hasattr(record, "_daiquiri_extra_keys"):
            record._daiquiri_extra_keys = sorted(record._daiquiri_extra_keys)
        return super().format(record)

    def add_extras(self, record):
        super().add_extras(record)
        worker_id = WORKER_ID.get(None)
        if worker_id is not None:
            record.extras += " " + self.extras_template.format("worker_id", worker_id)


CUSTOM_FORMATTER = CustomFormatter(
    fmt="%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s %(name)s: \033[1m%(message)s\033[0m%(extras)s%(color_stop)s"
)


class HerokuDatadogFormatter(daiquiri.formatter.DatadogFormatter):  # type: ignore [misc]
    HEROKU_LOG_EXTRAS = {
        envvar: os.environ[envvar]
        for envvar in ("HEROKU_RELEASE_VERSION", "HEROKU_SLUG_COMMIT")
        if envvar in os.environ
    }

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record.update(self.HEROKU_LOG_EXTRAS)
        log_record.update(ddtrace.tracer.get_log_correlation_context())
        worker_id = WORKER_ID.get(None)
        if worker_id is not None:
            log_record.update({"worker_id": worker_id})


def config_log() -> None:
    LOG.info("##################### CONFIGURATION ######################")
    for key, value in config.CONFIG.items():
        name = str(key)
        if (
            name == "OAUTH_CLIENT_ID"
            or "PRIVATE_KEY" in name
            or "TOKEN" in name
            or "SECRET" in name
            or "PRIVATE_KEY" in name
        ) and value is not None:
            value = "*****"
        if "URL" in name and value is not None:
            value = re.sub(r"://[^@]*@", "://*****@", value)
        LOG.info("* MERGIFYENGINE_%s: %s", name, value)
    LOG.info("* PATH: %s", os.environ.get("PATH"))
    LOG.info("##########################################################")

    legacy_api_url = os.getenv("MERGIFYENGINE_GITHUB_API_URL")
    if legacy_api_url is not None:
        if legacy_api_url[-1] == "/":
            legacy_api_url = legacy_api_url[:-1]
        if legacy_api_url.endswith("/api/v3"):
            LOG.warning(
                """
MERGIFYENGINE_GITHUB_API_URL configuration environment is deprecated and must be replaced by:

  MERGIFYENGINE_GITHUB_REST_API_URL=%s
  MERGIFYENGINE_GITHUB_GRAPHQL_API_URL=%s

  """,
                legacy_api_url,
                f"{legacy_api_url[:-3]}/graphql",
            )


def setup_logging(dump_config: bool = True) -> None:
    outputs = []

    if config.LOG_STDOUT:
        outputs.append(
            daiquiri.output.Stream(
                sys.stdout, level=config.LOG_STDOUT_LEVEL, formatter=CUSTOM_FORMATTER
            )
        )

    if config.LOG_DATADOG:
        outputs.append(
            daiquiri.output.Datadog(
                level=config.LOG_DATADOG_LEVEL,
                handler_class=daiquiri.handlers.PlainTextDatagramHandler,
                formatter=HerokuDatadogFormatter(),
            )
        )

    daiquiri.setup(
        outputs=outputs,
        level=config.LOG_LEVEL,
    )
    daiquiri.set_default_log_levels(
        [
            ("github.Requester", "WARN"),
            ("urllib3.connectionpool", "WARN"),
            ("urllib3.util.retry", "WARN"),
            ("vcr", "WARN"),
            ("httpx", "WARN"),
            ("asyncio", "WARN"),
            ("uvicorn.access", "WARN"),
        ]
        + [(name, "DEBUG") for name in config.LOG_DEBUG_LOGGER_NAMES]
    )

    if dump_config:
        config_log()
