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
import secrets
import sys
import typing
from urllib import parse

import dotenv
import voluptuous

from mergify_engine import github_types


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


def CommaSeparatedStringTuple(
    v: str, split: int = 2
) -> typing.List[typing.Tuple[str, ...]]:
    d = []
    for bot in v.split(","):
        if bot.strip():
            values = bot.split(":", maxsplit=split)
            if len(values) != split:
                raise ValueError("not enough :")
            d.append(tuple(v.strip() for v in values))
    return d


def AccountTokens(v: str) -> typing.List[typing.Tuple[int, str, str]]:
    try:
        return [
            (int(_id), login, token)
            for _id, login, token in typing.cast(
                typing.List[typing.Tuple[int, str, str]],
                CommaSeparatedStringTuple(v, split=3),
            )
        ]
    except ValueError:
        raise ValueError("wrong format, expect `id1:login1:token1,id2:login2:token2`")


API_ACCESS_KEY_LEN = 32
API_SECRET_KEY_LEN = 32


class ApplicationAPIKey(typing.TypedDict):
    api_secret_key: str
    api_access_key: str
    account_id: int
    account_login: str


def ApplicationAPIKeys(v: str) -> typing.Dict[str, ApplicationAPIKey]:
    try:
        applications = CommaSeparatedStringTuple(v, 3)
        for api_key, _, _ in applications:
            if len(api_key) != API_ACCESS_KEY_LEN + API_ACCESS_KEY_LEN:
                raise ValueError

        return {
            api_key[:API_ACCESS_KEY_LEN]: {
                "api_access_key": api_key[:API_ACCESS_KEY_LEN],
                "api_secret_key": api_key[API_ACCESS_KEY_LEN:],
                "account_id": int(account_id),
                "account_login": account_login,
            }
            for api_key, account_id, account_login in applications
        }
    except ValueError:
        raise ValueError(
            "wrong format, expect `api_key1:github_account_id1:github_account_login1,api_key1:github_account_id2:github_account_login2`, api_key must be 64 character long"
        )


Schema = voluptuous.Schema(
    {
        voluptuous.Required(
            "VERSION", default=os.getenv("HEROKU_SLUG_COMMIT", "dev")
        ): str,
        voluptuous.Required("SAAS_MODE", default=False): CoercedBool,
        # Logging
        voluptuous.Required(
            "LOG_DEBUG_LOGGER_NAMES", default=""
        ): CommaSeparatedStringList,
        voluptuous.Required("API_ENABLE", default=False): CoercedBool,
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
        voluptuous.Required("INTEGRATION_ID"): voluptuous.Coerce(int),
        voluptuous.Required("PRIVATE_KEY"): str,
        voluptuous.Required("OAUTH_CLIENT_ID"): str,
        voluptuous.Required("OAUTH_CLIENT_SECRET"): str,
        voluptuous.Required("WEBHOOK_SECRET"): str,
        voluptuous.Required(
            "WEBHOOK_SECRET_PRE_ROTATION", default=None
        ): voluptuous.Any(None, str),
        # GitHub common
        voluptuous.Required("BOT_USER_ID"): voluptuous.Coerce(int),
        voluptuous.Required("BOT_USER_LOGIN"): str,
        # GitHub optional
        voluptuous.Required("GITHUB_URL", default="https://github.com"): str,
        voluptuous.Required(
            "GITHUB_REST_API_URL", default="https://api.github.com"
        ): str,
        voluptuous.Required(
            "GITHUB_GRAPHQL_API_URL", default="https://api.github.com/graphql"
        ): str,
        #
        # Dashboard settings
        #
        voluptuous.Required(
            "SUBSCRIPTION_BASE_URL", default="https://subscription.mergify.com"
        ): str,
        # OnPremise special config
        voluptuous.Required("SUBSCRIPTION_TOKEN", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("ACCOUNT_TOKENS", default=""): voluptuous.Coerce(
            AccountTokens
        ),
        voluptuous.Required("APPLICATION_APIKEYS", default=""): voluptuous.Coerce(
            ApplicationAPIKeys
        ),
        # Saas Special config
        voluptuous.Required("ENGINE_TO_DASHBOARD_API_KEY"): str,
        voluptuous.Required("DASHBOARD_TO_ENGINE_API_KEY"): str,
        voluptuous.Required(
            "DASHBOARD_TO_ENGINE_API_KEY_PRE_ROTATION", default=None
        ): voluptuous.Any(None, str),
        voluptuous.Required("WEBHOOK_APP_FORWARD_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required(
            "WEBHOOK_MARKETPLACE_FORWARD_URL", default=None
        ): voluptuous.Any(None, str),
        voluptuous.Required(
            "WEBHOOK_FORWARD_EVENT_TYPES", default=None
        ): voluptuous.Any(None, CommaSeparatedStringList),
        #
        # Mergify Engine settings
        #
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
        voluptuous.Required(
            "REDIS_QUEUE_WEB_MAX_CONNECTIONS", default=None
        ): voluptuous.Any(None, voluptuous.Coerce(int)),
        voluptuous.Required(
            "REDIS_EVENTLOGS_WEB_MAX_CONNECTIONS", default=None
        ): voluptuous.Any(None, voluptuous.Coerce(int)),
        # NOTE(sileht): Unused anymore, but keep to detect legacy onpremise installation
        voluptuous.Required("STORAGE_URL", default=None): voluptuous.Any(None, str),
        # NOTE(sileht): Not used directly, but used to build other redis urls if not provided
        voluptuous.Required("DEFAULT_REDIS_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("LEGACY_CACHE_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("QUEUE_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("STREAM_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("TEAM_MEMBERS_CACHE_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("TEAM_PERMISSIONS_CACHE_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("USER_PERMISSIONS_CACHE_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("ACTIVE_USERS_URL", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("EVENTLOGS_URL", default=None): voluptuous.Any(None, str),
        voluptuous.Required("SHARED_STREAM_PROCESSES", default=1): voluptuous.Coerce(
            int
        ),
        voluptuous.Required(
            "SHARED_STREAM_TASKS_PER_PROCESS", default=7
        ): voluptuous.Coerce(int),
        voluptuous.Required(
            "BUCKET_PROCESSING_MAX_SECONDS", default=30
        ): voluptuous.Coerce(int),
        voluptuous.Required("CACHE_TOKEN_SECRET"): str,
        voluptuous.Required("CACHE_TOKEN_SECRET_OLD", default=None): voluptuous.Any(
            None, str
        ),
        voluptuous.Required("WORKER_SHUTDOWN_TIMEOUT", default=10): voluptuous.Coerce(
            float
        ),
        # For test suite only (eg: tox -erecord)
        voluptuous.Required(
            "TESTING_FORWARDER_ENDPOINT",
            default="https://test-forwarder.mergify.io/events",
        ): str,
        voluptuous.Required(
            "TESTING_INSTALLATION_ID", default=15398551
        ): voluptuous.Coerce(int),
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
        voluptuous.Required("ORG_ADMIN_ID", default=38494943): int,
        voluptuous.Required(
            "ORG_ADMIN_PERSONAL_TOKEN",
            default="<ORG_ADMIN_PERSONAL_TOKEN>",
        ): str,
        voluptuous.Required(
            "EXTERNAL_USER_PERSONAL_TOKEN", default="<EXTERNAL_USER_TOKEN>"
        ): str,
        voluptuous.Required("ORG_USER_ID", default=74646794): int,
        voluptuous.Required("ORG_USER_PERSONAL_TOKEN", default="<ORG_USER_TOKEN>"): str,
        voluptuous.Required(
            "TESTING_MERGIFY_TEST_1_ID", default=38494943
        ): voluptuous.Coerce(int),
        voluptuous.Required(
            "TESTING_MERGIFY_TEST_2_ID", default=38495008
        ): voluptuous.Coerce(int),
        "TESTING_GPGKEY_SECRET": str,
        "TESTING_ID_GPGKEY_SECRET": str,
    }
)

# Config variables available from voluptuous
VERSION: str
BASE_URL: str
API_ENABLE: bool
SENTRY_URL: str
SENTRY_ENVIRONMENT: str
CACHE_TOKEN_SECRET: str
CACHE_TOKEN_SECRET_OLD: typing.Optional[str]
PRIVATE_KEY: bytes
GITHUB_URL: str
GITHUB_REST_API_URL: str
GITHUB_GRAPHQL_API_URL: str
WEBHOOK_MARKETPLACE_FORWARD_URL: str
WEBHOOK_APP_FORWARD_URL: str
WEBHOOK_FORWARD_EVENT_TYPES: str
WEBHOOK_SECRET: str
WEBHOOK_SECRET_PRE_ROTATION: typing.Optional[str]
SHARED_STREAM_PROCESSES: int
SHARED_STREAM_TASKS_PER_PROCESS: int
EXTERNAL_USER_PERSONAL_TOKEN: str
BOT_USER_ID: github_types.GitHubAccountIdType
BOT_USER_LOGIN: github_types.GitHubLogin

STREAM_URL: str
EVENTLOGS_URL: str
QUEUE_URL: str
LEGACY_CACHE_URL: str
TEAM_PERMISSIONS_CACHE_URL: str
TEAM_MEMBERS_CACHE_URL: str
USER_PERMISSIONS_CACHE_URL: str
ACTIVE_USERS_URL: str

BUCKET_PROCESSING_MAX_SECONDS: int
INTEGRATION_ID: int
SUBSCRIPTION_BASE_URL: str
SUBSCRIPTION_TOKEN: str
ENGINE_TO_DASHBOARD_API_KEY: str
DASHBOARD_TO_ENGINE_API_KEY: str
DASHBOARD_TO_ENGINE_API_KEY_PRE_ROTATION: str
OAUTH_CLIENT_ID: str
OAUTH_CLIENT_SECRET: str
ACCOUNT_TOKENS: typing.List[typing.Tuple[int, str, str]]
APPLICATION_APIKEYS: typing.Dict[str, ApplicationAPIKey]
WORKER_SHUTDOWN_TIMEOUT: float
REDIS_SSL_VERIFY_MODE_CERT_NONE: bool
REDIS_STREAM_WEB_MAX_CONNECTIONS: typing.Optional[int]
REDIS_CACHE_WEB_MAX_CONNECTIONS: typing.Optional[int]
REDIS_QUEUE_WEB_MAX_CONNECTIONS: typing.Optional[int]
REDIS_EVENTLOGS_WEB_MAX_CONNECTIONS: typing.Optional[int]
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
ORG_ADMIN_ID: github_types.GitHubAccountIdType
ORG_USER_ID: github_types.GitHubAccountIdType
ORG_USER_PERSONAL_TOKEN: github_types.GitHubOAuthToken
TESTING_MERGIFY_TEST_1_ID: int
TESTING_MERGIFY_TEST_2_ID: int
TESTING_GPGKEY_SECRET: bytes
TESTING_ID_GPGKEY_SECRET: str
TESTING_INSTALLATION_ID: int
SAAS_MODE: bool

# config variables built
GITHUB_DOMAIN: str


def load() -> typing.Dict[str, typing.Any]:
    configuration_file = os.getenv("MERGIFYENGINE_TEST_SETTINGS")

    if configuration_file is not None:
        dotenv.load_dotenv(dotenv_path=configuration_file, override=True)

    raw_config: typing.Dict[str, typing.Any] = {}
    for key, _ in Schema.schema.items():
        val = os.getenv(f"MERGIFYENGINE_{key}")
        if val is not None:
            raw_config[key] = val

    # DASHBOARD API KEYS are required only for Saas
    if "SUBSCRIPTION_TOKEN" in raw_config:
        for key in ("DASHBOARD_TO_ENGINE_API_KEY", "ENGINE_TO_DASHBOARD_API_KEY"):
            raw_config[key] = secrets.token_hex(16)

    legacy_api_url = os.getenv("MERGIFYENGINE_GITHUB_API_URL")
    if legacy_api_url is not None:
        if legacy_api_url[-1] == "/":
            legacy_api_url = legacy_api_url[:-1]
        if legacy_api_url.endswith("/api/v3"):
            raw_config["GITHUB_REST_API_URL"] = legacy_api_url
            raw_config["GITHUB_GRAPHQL_API_URL"] = f"{legacy_api_url[:-3]}/graphql"

    parsed_config = Schema(raw_config)

    # NOTE(sileht): on legacy on-premise installation, before things were auto
    # sharded in redis databases, STREAM/QUEUE/LEGACY_CACHE was in the same db, so
    # keep until manual migration as been done
    if parsed_config["STORAGE_URL"] is not None:
        for config_key in (
            "DEFAULT_REDIS_URL",
            "STREAM_URL",
            "QUEUE_URL",
            "ACTIVE_USERS_URL",
            "LEGACY_CACHE_URL",
        ):
            if parsed_config[config_key] is None:
                parsed_config[config_key] = parsed_config["STORAGE_URL"]

    # NOTE(sileht): If we reach 15, we should update onpremise installation guide
    # and add an upgrade release note section to ensure people configure their Redis
    # correctly
    REDIS_AUTO_DB_SHARDING_MAPPING = {
        # 0 reserved, never use it, this force people to select a DB before running any command
        # and maybe be used by legacy onpremise installation.
        # 1 temporary reserved, used by dashboard
        "LEGACY_CACHE_URL": 2,
        "STREAM_URL": 3,
        "QUEUE_URL": 4,
        "TEAM_MEMBERS_CACHE_URL": 5,
        "TEAM_PERMISSIONS_CACHE_URL": 6,
        "USER_PERMISSIONS_CACHE_URL": 7,
        "EVENTLOGS_URL": 8,
        "ACTIVE_USERS_URL": 9,
    }

    default_redis_url_parsed = parse.urlparse(
        parsed_config["DEFAULT_REDIS_URL"] or "redis://localhost:6379"
    )
    if default_redis_url_parsed.query and "db" in parse.parse_qs(
        default_redis_url_parsed.query
    ):
        print(
            "DEFAULT_REDIS_URL must not contain any db parameter. Mergify can't start."
        )
        sys.exit(1)

    for config_key, db in REDIS_AUTO_DB_SHARDING_MAPPING.items():
        if parsed_config[config_key] is None:
            query = default_redis_url_parsed.query
            if query:
                query += "&"
            query += f"db={db}"
            url = default_redis_url_parsed._replace(query=query).geturl()
            parsed_config[config_key] = url

    parsed_config["GITHUB_DOMAIN"] = parse.urlparse(
        parsed_config["GITHUB_URL"]
    ).hostname

    # NOTE(sileht): Docker can't pass multiline in environment, so we allow to pass
    # it in base64 format
    if not parsed_config["PRIVATE_KEY"].startswith("----"):
        parsed_config["PRIVATE_KEY"] = base64.b64decode(parsed_config["PRIVATE_KEY"])

    if "TESTING_GPGKEY_SECRET" in parsed_config and not parsed_config[
        "TESTING_GPGKEY_SECRET"
    ].startswith("----"):
        parsed_config["TESTING_GPGKEY_SECRET"] = base64.b64decode(
            parsed_config["TESTING_GPGKEY_SECRET"]
        )

    if not parsed_config["SAAS_MODE"] and not parsed_config["SUBSCRIPTION_TOKEN"]:
        print("SUBSCRIPTION_TOKEN is missing. Mergify can't start.")
        sys.exit(1)

    return parsed_config  # type: ignore[no-any-return]


CONFIG = load()
globals().update(CONFIG)
