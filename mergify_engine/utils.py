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

import datetime
import hashlib
import hmac
import logging
import shutil
import subprocess
import sys
import tempfile

import aioredis
from billiard import current_process
import celery.app.log
import daiquiri
import redis

from mergify_engine import config


LOG = daiquiri.getLogger(__name__)


global AIOREDIS_CONNECTION_CACHE
AIOREDIS_CONNECTION_CACHE = None


async def get_aioredis_for_cache():
    global AIOREDIS_CONNECTION_CACHE
    if AIOREDIS_CONNECTION_CACHE is None:
        AIOREDIS_CONNECTION_CACHE = await aioredis.create_redis_pool(
            config.STORAGE_URL, encoding="utf8"
        )
        p = current_process()
        await AIOREDIS_CONNECTION_CACHE.client_setname("cache:%s" % p.name)
    return AIOREDIS_CONNECTION_CACHE


global REDIS_CONNECTION_CACHE
REDIS_CONNECTION_CACHE = None


def get_redis_for_cache():
    global REDIS_CONNECTION_CACHE
    if REDIS_CONNECTION_CACHE is None:
        REDIS_CONNECTION_CACHE = redis.StrictRedis.from_url(
            config.STORAGE_URL, decode_responses=True,
        )
        p = current_process()
        REDIS_CONNECTION_CACHE.client_setname("cache:%s" % p.name)
    return REDIS_CONNECTION_CACHE


def utcnow():
    return datetime.datetime.now(tz=datetime.timezone.utc)


def unicode_truncate(s, length, encoding="utf-8"):
    """Truncate a string to length in bytes.

    :param s: The string to truncate.
    :param length: The length in number of bytes — not characters."""
    return s.encode(encoding)[:length].decode(encoding, errors="ignore")


class CustomFormatter(
    daiquiri.formatter.ColorExtrasFormatter, celery.app.log.TaskFormatter
):
    pass


CELERY_EXTRAS_FORMAT = (
    "%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s "
    "[%(task_id)s] "
    "%(name)s%(extras)s: %(message)s%(color_stop)s"
)


def setup_logging():
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
            ("uvicorn.access", "WARN"),
        ]
    )

    config.log()


def compute_hmac(data):
    mac = hmac.new(
        config.WEBHOOK_SECRET.encode("utf8"), msg=data, digestmod=hashlib.sha1
    )
    return str(mac.hexdigest())


class Gitter(object):
    def __init__(self, logger):
        self.tmp = tempfile.mkdtemp(prefix="mergify-gitter")
        self.logger = logger
        self.logger.info("working in: %s", self.tmp)

    def __call__(self, *args, **kwargs):  # pragma: no cover
        self.logger.info("calling: %s", " ".join(args))
        kwargs["cwd"] = self.tmp
        kwargs["stderr"] = subprocess.STDOUT
        # Worker timeout at 5 minutes, so ensure subprocess return before
        kwargs["timeout"] = 4 * 60 + 30
        try:
            return subprocess.check_output(["git"] + list(args), **kwargs)
        except subprocess.CalledProcessError as e:
            self.logger.info("output: %s", e.output)
            raise

    def cleanup(self):
        self.logger.info("cleaning: %s", self.tmp)
        try:
            self("credential-cache", "--socket=%s/.git/creds/socket" % self.tmp, "exit")
        except subprocess.CalledProcessError:  # pragma: no cover
            self.logger.warning("git credential-cache exit fail")
        shutil.rmtree(self.tmp)

    def configure(self):
        self("config", "user.name", "%s-bot" % config.CONTEXT)
        self("config", "user.email", config.GIT_EMAIL)
        # Use one git cache daemon per Gitter
        self("config", "credential.useHttpPath", "true")
        self(
            "config",
            "credential.helper",
            "cache --timeout=300 --socket=%s/.git/creds/socket" % self.tmp,
        )

    def add_cred(self, username, password, path):
        domain = config.GITHUB_DOMAIN
        self(
            "credential",
            "approve",
            input=(
                "url=https://%s:%s@%s/%s\n\n" % (username, password, domain, path)
            ).encode("utf8"),
        )
