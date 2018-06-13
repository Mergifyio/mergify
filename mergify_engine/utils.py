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

import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import sys
import tempfile

import daiquiri
from github import GithubException
import redis
import requests

from mergify_engine import config

LOG = logging.getLogger(__name__)


global REDIS_CONNECTION
REDIS_CONNECTION = None


def get_redis_url():
    for envvar in ["REDIS_URL", "REDISTOGO_URL", "REDISCLOUD_URL",
                   "PIFPAF_URL"]:
        redis_url = os.getenv(envvar)
        if redis_url:
            break
    if not redis_url:
        raise RuntimeError("No redis url found in environments")
    return redis_url


def get_redis():
    global REDIS_CONNECTION
    if REDIS_CONNECTION is None:
        REDIS_CONNECTION = redis.from_url(get_redis_url())
    return REDIS_CONNECTION


def setup_logging():
    daiquiri.setup(
        outputs=[daiquiri.output.Stream(
            sys.stdout,
            formatter=daiquiri.formatter.ColorFormatter(
                "%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s "
                "%(name)s: %(message)s%(color_stop)s"),
        )],
        level=(logging.DEBUG if config.DEBUG else logging.INFO),
    )
    daiquiri.set_default_log_levels([
        ("rq", "ERROR"),
        # ("github.Requester", "DEBUG"),
    ])


def compute_hmac(data):
    mac = hmac.new(config.WEBHOOK_SECRET.encode("utf8"),
                   msg=data, digestmod=hashlib.sha1)
    return str(mac.hexdigest())


def get_installations(integration):
    # FIXME(sileht): Need to be in github libs
    response = requests.get(
        "https://api.github.com/app/installations",
        headers={
            "Authorization": "Bearer {}".format(integration.create_jwt()),
            "Accept": "application/vnd.github.machine-man-preview+json",
            "User-Agent": "PyGithub/Python"
        },
    )
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 403:
        raise GithubException.BadCredentialsException(
            status=response.status_code,
            data=response.text
        )
    elif response.status_code == 404:
        raise GithubException.UnknownObjectException(
            status=response.status_code,
            data=response.text
        )
    raise GithubException.GithubException(
        status=response.status_code,
        data=response.text
    )


def get_installation_id(integration, owner):
    installations = get_installations(integration)
    for install in installations:
        if install["account"]["login"] == owner:
            return install["id"]


def get_subscription(r, installation_id):
    sub = r.hgetall("subscription-cache-%d" % installation_id)
    if sub is None:
        LOG.info("Subscription for %s not cached, retrieving it..." %
                 installation_id)
        resp = requests.get("https://mergify.io/engine/token/%s" %
                            installation_id,
                            auth=(config.OAUTH_CLIENT_ID,
                                  config.OAUTH_CLIENT_SECRET))
        if resp.status_code == 404:
            sub = {
                "token": None,
                "subscribed": False
            }
        elif resp.status_code == 200:
            sub = resp.json()
            sub["subscribed"] = sub["subscription"] is not None
            del sub["subscription"]
        else:
            # NOTE(sileht): handle this better
            resp.raise_for_status()
        r.hmset("subscription-cache-%s" % installation_id, sub)
    else:
        return sub


class Gitter(object):
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="mergify-gitter")
        LOG.info("working in: %s" % self.tmp)

    def __call__(self, *args, **kwargs):
        LOG.info("calling: %s" % " ".join(args))
        kwargs["cwd"] = self.tmp
        return subprocess.check_output(["git"] + list(args), **kwargs)

    def cleanup(self):
        LOG.info("cleaning: %s" % self.tmp)
        shutil.rmtree(self.tmp)
