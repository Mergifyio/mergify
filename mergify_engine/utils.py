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
import shutil
import subprocess
import tempfile
import urllib.parse

import aredis
from billiard import current_process
import redis

from mergify_engine import config
from mergify_engine import logs


LOG = logs.getLogger(__name__)


async def create_aredis_for_cache():
    r = aredis.StrictRedis.from_url(config.STORAGE_URL, decode_responses=True)
    p = current_process()
    await r.client_setname("cache:%s" % p.name)
    return r


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


async def create_aredis_for_stream():
    r = aredis.StrictRedis.from_url(config.STREAM_URL)
    p = current_process()
    await r.client_setname("stream:%s" % p.name)
    return r


def utcnow():
    return datetime.datetime.now(tz=datetime.timezone.utc)


def unicode_truncate(s, length, encoding="utf-8"):
    """Truncate a string to length in bytes.

    :param s: The string to truncate.
    :param length: The length in number of bytes — not characters."""
    return s.encode(encoding)[:length].decode(encoding, errors="ignore")


def compute_hmac(data):
    mac = hmac.new(
        config.WEBHOOK_SECRET.encode("utf8"), msg=data, digestmod=hashlib.sha1
    )
    return str(mac.hexdigest())


def get_pull_logger(pull):
    return logs.getLogger(
        __name__,
        gh_owner=pull["base"]["user"]["login"] if "user" in pull else "<unknown-yet>",
        gh_repo=(pull["base"]["repo"]["name"] if "base" in pull else "<unknown-yet>"),
        gh_private=(
            pull["base"]["repo"]["private"] if "base" in pull else "<unknown-yet>"
        ),
        gh_branch=pull["base"]["ref"] if "base" in pull else "<unknown-yet>",
        gh_pull=pull["number"],
        gh_pull_url=pull.get("html_url", "<unknown-yet>"),
        gh_pull_state=(
            "merged"
            if pull.get("merged")
            else (pull.get("mergeable_state", "unknown") or "none")
        ),
    )


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
        finally:
            self.logger.debug("finish: %s", " ".join(args))

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
        parsed = list(urllib.parse.urlparse(config.GITHUB_URL))
        parsed[1] = f"{username}:{password}@{parsed[1]}"
        parsed[2] = path
        url = urllib.parse.urlunparse(parsed)
        self("credential", "approve", input=f"url={url}\n\n".encode("utf8"))
