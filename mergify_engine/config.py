# -*- encoding: utf-8 -*-
#
# Copyright © 2017 Red Hat, Inc.
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

import base64
import distutils.util
import logging
import os
import re

import daiquiri
import dotenv
import voluptuous


LOG = daiquiri.getLogger(__name__)


# NOTE(sileht) we coerce bool and int in case their are loaded from environment
def CoercedBool(value):
    return bool(distutils.util.strtobool(str(value)))


def CoercedLoggingLevel(value):
    value = value.upper()
    if value in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        return getattr(logging, value)
    raise ValueError(value)


def CommaSeparatedStringList(value):
    return value.split(",")


def CommaSeparatedIntList(value):
    return [int(s) for s in value.split(",")]


Schema = voluptuous.Schema(
    {
        # Logging
        voluptuous.Required("DEBUG", default=True): CoercedBool,
        voluptuous.Required("LOG_RATELIMIT", default=False): CoercedBool,
        voluptuous.Required("LOG_STDOUT", default=True): CoercedBool,
        voluptuous.Required("LOG_STDOUT_LEVEL", default=None): voluptuous.Any(
            None, CoercedLoggingLevel
        ),
        voluptuous.Required("LOG_DATADOG", default=False): CoercedBool,
        voluptuous.Required("SENTRY_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("SENTRY_ENVIRONMENT", default="test"): str,
        # Github mandatory
        voluptuous.Required("INTEGRATION_ID"): voluptuous.Coerce(int),
        voluptuous.Required("PRIVATE_KEY"): str,
        voluptuous.Required("BOT_USER_ID"): voluptuous.Coerce(int),
        voluptuous.Required("OAUTH_CLIENT_ID"): str,
        voluptuous.Required("OAUTH_CLIENT_SECRET"): str,
        voluptuous.Required("WEBHOOK_SECRET"): str,
        # Github optional
        voluptuous.Required("GITHUB_DOMAIN", default="github.com"): str,
        # Mergify website for subscription
        voluptuous.Required(
            "SUBSCRIPTION_URL", default="http://localhost:5000/engine/installation/%s"
        ): str,
        voluptuous.Required("WEBHOOK_APP_FORWARD_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required(
            "WEBHOOK_MARKETPLACE_FORWARD_URL", default=None
        ): voluptuous.Any(None, str),
        voluptuous.Required(
            "WEBHOOK_FORWARD_EVENT_TYPES", default=None
        ): voluptuous.Any(None, CommaSeparatedStringList),
        # Mergify
        voluptuous.Required("BASE_URL", default="http://localhost:8802"): str,
        voluptuous.Required("STORAGE_URL", default="redis://localhost:6379?db=8"): str,
        voluptuous.Required("STREAM_URL", default="redis://localhost:6379?db=7"): str,
        voluptuous.Required("STREAM_WORKERS", default=7): int,
        voluptuous.Required(
            "AB_TESTING_INSTALLATION_IDS", default="499592"
        ): voluptuous.Any(None, CommaSeparatedIntList),
        voluptuous.Required("AB_TESTING_PERCENTAGE", default=1): voluptuous.Any(
            None, int
        ),
        voluptuous.Required("CACHE_TOKEN_SECRET"): str,
        voluptuous.Required(
            "CELERY_BROKER_URL", default="redis://localhost:6379/9"
        ): str,
        voluptuous.Required("CONTEXT", default="mergify"): str,
        voluptuous.Required("GIT_EMAIL", default="noreply@mergify.io"): str,
        voluptuous.Required("NOSUB_MAX_REPO_SIZE", default=512 * 1024 * 1024): int,
        # For test suite only (eg: tox -erecord)
        voluptuous.Required("INSTALLATION_ID", default=499592): voluptuous.Coerce(int),
        voluptuous.Required("TESTING_ORGANIZATION", default="mergifyio-testing"): str,
        voluptuous.Required("MAIN_TOKEN", default="<MAIN_TOKEN>"): str,
        voluptuous.Required("FORK_TOKEN", default="<FORK_TOKEN>"): str,
        voluptuous.Required("MAIN_TOKEN_DELETE", default="<unused>"): str,
        voluptuous.Required("FORK_TOKEN_DELETE", default="<unused>"): str,
    }
)


configuration_file = os.getenv("MERGIFYENGINE_TEST_SETTINGS")
if configuration_file:
    dotenv.load_dotenv(stream=configuration_file, override=True)

CONFIG = {}
for key, value in Schema.schema.items():
    val = os.getenv("MERGIFYENGINE_%s" % key)
    if val is not None:
        CONFIG[key] = val

globals().update(Schema(CONFIG))

# NOTE(sileht): Docker can't pass multiline in environment, so we allow to pass
# it in base64 format
if not CONFIG["PRIVATE_KEY"].startswith("----"):
    CONFIG["PRIVATE_KEY"] = base64.b64decode(CONFIG["PRIVATE_KEY"])
    PRIVATE_KEY = CONFIG["PRIVATE_KEY"]


def log():
    LOG.info("##################### CONFIGURATION ######################")
    for key, value in CONFIG.items():
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
