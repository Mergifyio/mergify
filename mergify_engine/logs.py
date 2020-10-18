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


import logging
import re
import sys

import daiquiri
import daiquiri.formatter

from mergify_engine import config


LOG = daiquiri.getLogger(__name__)

logging.addLevelName(42, "TEST")
LEVEL_COLORS = daiquiri.formatter.ColorFormatter.LEVEL_COLORS.copy()
LEVEL_COLORS[42] = "\033[01;35m"


class CustomFormatter(daiquiri.formatter.ColorExtrasFormatter):
    LEVEL_COLORS = LEVEL_COLORS

    def format(self, record):
        if hasattr(record, "_daiquiri_extra_keys"):
            record._daiquiri_extra_keys = sorted(record._daiquiri_extra_keys)
        return super().format(record)


CUSTOM_FORMATTER = CustomFormatter(
    fmt="%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s %(name)s: %(message)s%(extras)s%(color_stop)s"
)


def config_log():
    LOG.info("##################### CONFIGURATION ######################")
    for key, value in config.CONFIG.items():
        name = str(key)
        if (
            name
            in [
                "PRIVATE_KEY",
                "WEBHOOK_SECRET",
                "CACHE_TOKEN_SECRET",
                "OAUTH_CLIENT_ID",
                "OAUTH_CLIENT_SECRET",
                "MAIN_TOKEN",
                "FORK_TOKEN",
                "MAIN_TOKEN_DELETE",
                "FORK_TOKEN_DELETE",
            ]
            and value is not None
        ):
            value = "*****"
        if "URL" in name and value is not None:
            value = re.sub(r"://[^@]*@", "://*****@", value)
        LOG.info("* MERGIFYENGINE_%s: %s", name, value)
    LOG.info("##########################################################")


def setup_logging():
    outputs = []

    if config.LOG_STDOUT:
        outputs.append(
            daiquiri.output.Stream(
                sys.stdout, level=config.LOG_STDOUT_LEVEL, formatter=CUSTOM_FORMATTER
            )
        )

    if config.LOG_DATADOG:
        outputs.append(daiquiri.output.Datadog(level=config.LOG_DATADOG_LEVEL))

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
    )

    config_log()
