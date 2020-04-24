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

import celery.app.log
import daiquiri

from mergify_engine import config


global GLOBAL_EXTRAS
GLOBAL_EXTRAS = {}


class CustomFormatter(
    daiquiri.formatter.ColorExtrasFormatter, celery.app.log.TaskFormatter
):
    pass


CELERY_EXTRAS_FORMAT = (
    "%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s "
    "[%(task_id)s] "
    "%(name)s%(extras)s: %(message)s%(color_stop)s"
)


def getLogger(name, **kwargs):
    global GLOBAL_EXTRAS
    extras = {}
    extras.update(GLOBAL_EXTRAS)
    extras.update(kwargs)
    return daiquiri.getLogger(name, **extras)


LOG = getLogger(__name__)


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


def setup_logging(*kwargs):
    global GLOBAL_EXTRAS
    GLOBAL_EXTRAS.update(kwargs)

    outputs = []

    if config.LOG_STDOUT:
        outputs.append(
            daiquiri.output.Stream(
                sys.stdout,
                formatter=CustomFormatter(fmt=CELERY_EXTRAS_FORMAT),
                level=config.LOG_STDOUT_LEVEL,
            )
        )

    if config.LOG_DATADOG:
        outputs.append(daiquiri.output.Datadog())

    daiquiri.setup(
        outputs=outputs, level=(logging.DEBUG if config.DEBUG else logging.INFO),
    )
    daiquiri.set_default_log_levels(
        [
            ("celery", "INFO"),
            ("kombu", "WARN"),
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
