# -*- encoding: utf-8 -*-
#
# Copyright © 2020–2021 Mergify SAS
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
import typing

import dotenv
import voluptuous

from mergify_engine import github_types


GITHUB_APP = os.environ.get("MERGIFYENGINE_MODE", "github_app") == "github_app"
if GITHUB_APP:
    GitHubAppRequired = voluptuous.Required
    GitHubActionRequired = voluptuous.Optional
else:
    GitHubAppRequired = voluptuous.Optional
    GitHubActionRequired = voluptuous.Required


# NOTE(sileht) we coerce bool and int in case they are loaded from the environment
def CoercedBool(value: typing.Any) -> bool:
    return bool(distutils.util.strtobool(str(value)))


def CoercedLoggingLevel(value: str) -> int:
    value = value.upper()
    if value in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        return int(getattr(logging, value))
    raise ValueError(value)


def CommaSeparatedStringList(value: str) -> typing.List[str]:
    if value:
        return value.split(",")
    else:
        return []


def AccountTokens(v: str) -> typing.Dict[str, str]:
    try:
        return dict(
            typing.cast(typing.Tuple[str, str], tuple(map(str.strip, bot.split(":"))))
            for bot in v.split(",")
            if bot.strip()
        )
    except ValueError:
        raise ValueError("wrong format, expect `login1:token1,login2:token2`")


Schema = voluptuous.Schema(
    {
        # Logging
        voluptuous.Required(
            "LOG_DEBUG_LOGGER_NAMES", default=""
        ): CommaSeparatedStringList,
        voluptuous.Required("LOG_LEVEL", default="INFO"): CoercedLoggingLevel,
        voluptuous.Required("LOG_RATELIMIT", default=False): CoercedBool,
        voluptuous.Required("LOG_STDOUT", default=True): CoercedBool,
        voluptuous.Required("LOG_STDOUT_LEVEL", default=None): voluptuous.Any(
            None, CoercedLoggingLevel
        ),
        voluptuous.Required("LOG_DATADOG", default=False): CoercedBool,
        voluptuous.Required("LOG_DATADOG_LEVEL", default=None): voluptuous.Any(
            None, CoercedLoggingLevel
        ),
        voluptuous.Required("SENTRY_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("SENTRY_ENVIRONMENT", default="test"): str,
        # GitHub App mandatory
        GitHubAppRequired("INTEGRATION_ID"): voluptuous.Coerce(int),
        GitHubAppRequired("PRIVATE_KEY"): str,
        GitHubAppRequired("OAUTH_CLIENT_ID"): str,
        GitHubAppRequired("OAUTH_CLIENT_SECRET"): str,
        GitHubAppRequired("WEBHOOK_SECRET"): str,
        # GitHub Action mandatory
        GitHubActionRequired("ACTION_ID"): voluptuous.Coerce(int),
        GitHubActionRequired("GITHUB_TOKEN"): str,
        # GitHub common
        voluptuous.Required("BOT_USER_ID"): voluptuous.Coerce(int),
        voluptuous.Required("BOT_USER_LOGIN"): str,
        # GitHub optional
        voluptuous.Required("GITHUB_URL", default="https://github.com"): str,
        voluptuous.Required("GITHUB_API_URL", default="https://api.github.com"): str,
        # Mergify website for subscription
        voluptuous.Required(
            "SUBSCRIPTION_BASE_URL", default="http://localhost:5000"
        ): str,
        voluptuous.Required("SUBSCRIPTION_TOKEN", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("ACCOUNT_TOKENS", default=""): voluptuous.Coerce(
            AccountTokens
        ),
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
        voluptuous.Required(
            "REDIS_SSL_VERIFY_MODE_CERT_NONE", default=False
        ): CoercedBool,
        voluptuous.Required(
            "REDIS_STREAM_WEB_MAX_CONNECTIONS", default=None
        ): voluptuous.Any(None, voluptuous.Coerce(int)),
        voluptuous.Required(
            "REDIS_CACHE_WEB_MAX_CONNECTIONS", default=None
        ): voluptuous.Any(None, voluptuous.Coerce(int)),
        voluptuous.Required("STORAGE_URL", default="redis://localhost:6379?db=8"): str,
        voluptuous.Required("STREAM_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("STREAM_PROCESSES", default=1): voluptuous.Coerce(int),
        voluptuous.Required("STREAM_WORKERS_PER_PROCESS", default=7): voluptuous.Coerce(
            int
        ),
        voluptuous.Required("STREAM_MAX_BATCH", default=100): voluptuous.Coerce(int),
        GitHubAppRequired("CACHE_TOKEN_SECRET"): str,
        voluptuous.Required("CONTEXT", default="mergify"): str,
        voluptuous.Required("GIT_EMAIL", default="noreply@mergify.io"): str,
        voluptuous.Required(
            "NOSUB_MAX_REPO_SIZE_KB", default=512 * 1000
        ): voluptuous.Coerce(int),
        voluptuous.Required("WORKER_SHUTDOWN_TIMEOUT", default=10): voluptuous.Coerce(
            float
        ),
        # For test suite only (eg: tox -erecord)
        voluptuous.Required(
            "TESTING_FORWARDER_ENDPOINT",
            default="https://test-forwarder.mergify.io/events",
        ): str,
        voluptuous.Required("INSTALLATION_ID", default=15398551): voluptuous.Coerce(
            int
        ),
        voluptuous.Required(
            "TESTING_REPOSITORY_ID", default=258840104
        ): voluptuous.Coerce(int),
        voluptuous.Required(
            "TESTING_REPOSITORY_NAME", default="functional-testing-repo"
        ): str,
        voluptuous.Required(
            "TESTING_ORGANIZATION_ID", default=40527191
        ): voluptuous.Coerce(int),
        voluptuous.Required(
            "TESTING_ORGANIZATION_NAME", default="mergifyio-testing"
        ): str,
        voluptuous.Required(
            "ORG_ADMIN_PERSONAL_TOKEN",
            default="<ORG_ADMIN_PERSONAL_TOKEN>",
        ): str,
        voluptuous.Required(
            "EXTERNAL_USER_PERSONAL_TOKEN", default="<EXTERNAL_USER_TOKEN>"
        ): str,
        voluptuous.Required(
            "ORG_USER_PERSONAL_TOKEN", default="<EXTERNAL_USER_TOKEN>"
        ): str,
        voluptuous.Required(
            "ORG_ADMIN_GITHUB_APP_OAUTH_TOKEN",
            default="<ORG_USER_GITHUB_APP_OAUTH_TOKEN>",
        ): str,
    }
)

# Config variables available
SENTRY_URL: str
SENTRY_ENVIRONMENT: str
CACHE_TOKEN_SECRET: str
PRIVATE_KEY: bytes
GITHUB_URL: str
GITHUB_API_URL: str
WEBHOOK_MARKETPLACE_FORWARD_URL: str
WEBHOOK_APP_FORWARD_URL: str
WEBHOOK_FORWARD_EVENT_TYPES: str
WEBHOOK_SECRET: str
STREAM_PROCESSES: int
STREAM_WORKERS_PER_PROCESS: int
EXTERNAL_USER_PERSONAL_TOKEN: str
BOT_USER_ID: int
BOT_USER_LOGIN: str
STORAGE_URL: str
STREAM_URL: str
STREAM_MAX_BATCH: int
INTEGRATION_ID: int
SUBSCRIPTION_BASE_URL: str
SUBSCRIPTION_TOKEN: str
OAUTH_CLIENT_ID: str
OAUTH_CLIENT_SECRET: str
ACTION_ID: int
NOSUB_MAX_REPO_SIZE_KB: int
GIT_EMAIL: str
CONTEXT: str
ACCOUNT_TOKENS: typing.Dict[str, str]
WORKER_SHUTDOWN_TIMEOUT: float
REDIS_SSL_VERIFY_MODE_CERT_NONE: bool
REDIS_STREAM_WEB_MAX_CONNECTIONS: typing.Optional[int]
REDIS_CACHE_WEB_MAX_CONNECTIONS: typing.Optional[int]
TESTING_ORGANIZATION_ID: github_types.GitHubAccountIdType
TESTING_ORGANIZATION_NAME: github_types.GitHubLogin
TESTING_REPOSITORY_ID: github_types.GitHubRepositoryIdType
TESTING_REPOSITORY_NAME: str
TESTING_FORWARDER_ENDPOINT: str
LOG_LEVEL: int  # This is converted to an int by voluptuous
LOG_STDOUT: bool
LOG_STDOUT_LEVEL: int  # This is converted to an int by voluptuous
LOG_DATADOG: bool
LOG_DATADOG_LEVEL: int  # This is converted to an int by voluptuous
LOG_DEBUG_LOGGER_NAMES: typing.List[str]
ORG_ADMIN_PERSONAL_TOKEN: str
ORG_ADMIN_GITHUB_APP_OAUTH_TOKEN: github_types.GitHubOAuthToken
ORG_USER_PERSONAL_TOKEN: github_types.GitHubOAuthToken

configuration_file = os.getenv("MERGIFYENGINE_TEST_SETTINGS")
if configuration_file:
    # FIXME: ignore type hit because it's wrong
    dotenv.load_dotenv(stream=configuration_file, override=True)  # type: ignore

CONFIG: typing.Dict[str, typing.Any] = {}
for key, _ in Schema.schema.items():
    val = os.getenv(f"MERGIFYENGINE_{key}")
    if val is not None:
        CONFIG[key] = val

globals().update(Schema(CONFIG))

if globals()["STREAM_URL"] is None:
    STREAM_URL = globals()["STORAGE_URL"]

# NOTE(sileht): Docker can't pass multiline in environment, so we allow to pass
# it in base64 format
if not CONFIG["PRIVATE_KEY"].startswith("----"):
    CONFIG["PRIVATE_KEY"] = base64.b64decode(CONFIG["PRIVATE_KEY"])
    PRIVATE_KEY = CONFIG["PRIVATE_KEY"]
