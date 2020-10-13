#
# Copyright © 2019–2020 Mergify SAS
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
import asyncio
import datetime
import hashlib
import hmac
import os
import shutil
import socket
import subprocess
import tempfile
import typing
import urllib.parse

import aredis
import redis

from mergify_engine import config


_PROCESS_IDENTIFIER = os.environ.get("DYNO") or socket.gethostname()


global AREDIS_CONNECTION_CACHE
AREDIS_CONNECTION_CACHE: typing.Optional[aredis.StrictRedis] = None


async def get_aredis_for_cache() -> aredis.StrictRedis:
    global AREDIS_CONNECTION_CACHE
    if AREDIS_CONNECTION_CACHE is None:
        AREDIS_CONNECTION_CACHE = aredis.StrictRedis.from_url(
            config.STORAGE_URL, decode_responses=True, max_idle_time=60
        )
        await AREDIS_CONNECTION_CACHE.client_setname("cache:%s" % _PROCESS_IDENTIFIER)
    return AREDIS_CONNECTION_CACHE


global REDIS_CONNECTION_CACHE
REDIS_CONNECTION_CACHE: typing.Optional[redis.StrictRedis] = None


def get_redis_for_cache() -> redis.StrictRedis:
    global REDIS_CONNECTION_CACHE
    if REDIS_CONNECTION_CACHE is None:
        REDIS_CONNECTION_CACHE = redis.StrictRedis.from_url(
            config.STORAGE_URL,
            decode_responses=True,
        )
        REDIS_CONNECTION_CACHE.client_setname("cache:%s" % _PROCESS_IDENTIFIER)
    return REDIS_CONNECTION_CACHE


async def create_aredis_for_stream() -> aredis.StrictRedis:
    r = aredis.StrictRedis.from_url(config.STREAM_URL, max_idle_time=60)
    await r.client_setname("stream:%s" % _PROCESS_IDENTIFIER)
    return r


async def stop_pending_aredis_tasks():
    tasks = [
        task
        for task in asyncio.all_tasks()
        if (
            task.get_coro().__qualname__
            == "ConnectionPool.disconnect_on_idle_time_exceeded"
        )
    ]

    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)


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


async def async_main(*awaitables):
    # NOTE(sileht): This looks useless but
    #   asyncio.run(asyncio.gather(...))
    # does not work, because when the gather() coroutine is created, it need the current
    # loop but asyncio.run() haven't create it yet, so it doesn't work.
    return await asyncio.gather(*awaitables, return_exceptions=True)


def async_run(*awaitables):
    return asyncio.run(async_main(*awaitables))


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


def get_random_choices(
    random_number: int, population: typing.Dict[typing.Any, int], k: int = 1
) -> set:
    """Return a random number of item from a population without replacement.

    You need to provide the random number yourself.

    The output is always the same based on that number.

    The population is a dict where the key is the choice and the value is the weight.

    The argument k is the number of item that should be picked.

    :param random_number: The random_number that should be picked.
    :param population: The dict of {item: weight}.
    :param k: The number of choices to make.
    :return: A set with the choices.
    """
    picked: typing.Set[typing.Any] = set()
    population = population.copy()

    if k > len(population):
        raise ValueError("k cannot be greater than the population size")

    while len(picked) < k:
        total_weight = sum(population.values())
        choice_index = (random_number % total_weight) + 1
        for item in sorted(population):
            choice_index -= population[item]
            if choice_index <= 0:
                picked.add(item)
                del population[item]
                break

    return picked
