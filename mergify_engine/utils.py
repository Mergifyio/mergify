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
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

import daiquiri
import github
import redis
import requests

from mergify_engine import config

LOG = logging.getLogger(__name__)


global REDIS_CONNECTION_RQ
REDIS_CONNECTION_RQ = None

global REDIS_CONNECTION_CACHE
REDIS_CONNECTION_CACHE = None


def get_redis_url():
    for envvar in ["PIFPAF_URL", "REDIS_URL",
                   "REDISTOGO_URL", "REDISCLOUD_URL"]:
        redis_url = os.getenv(envvar)
        if redis_url:
            break
    if not redis_url:
        redis_url = config.REDIS_URL
    if not redis_url:
        raise RuntimeError("No redis url found in environments")
    return redis_url


def get_redis_for_rq():
    global REDIS_CONNECTION_RQ
    if REDIS_CONNECTION_RQ is None:
        REDIS_CONNECTION_RQ = redis.StrictRedis.from_url(
            get_redis_url(), decode_responses=False)
    return REDIS_CONNECTION_RQ


def get_redis_for_cache():
    global REDIS_CONNECTION_CACHE
    if REDIS_CONNECTION_CACHE is None:
        REDIS_CONNECTION_CACHE = redis.StrictRedis.from_url(
            get_redis_url(), decode_responses=True)
    return REDIS_CONNECTION_CACHE


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
        ("rq", "WARN"),
        ("github.Requester", "WARN"),
        ("urllib3.connectionpool", "WARN"),
    ])


def compute_hmac(data):
    mac = hmac.new(config.WEBHOOK_SECRET.encode("utf8"),
                   msg=data, digestmod=hashlib.sha1)
    return str(mac.hexdigest())


def get_installations(integration):
    # FIXME(sileht): Need to be in github libs

    installs = []
    url = "https://api.github.com/app/installations"
    token = "Bearer {}".format(integration.create_jwt())
    session = requests.Session()
    while True:
        response = session.get(url, headers={
                "Authorization": token,
                "Accept": "application/vnd.github.machine-man-preview+json",
                "User-Agent": "PyGithub/Python"
            },
        )
        if response.status_code == 200:
            installs.extend(response.json())
            if "next" in response.links:
                url = response.links["next"]["url"]
                continue
            else:
                return installs
        elif response.status_code == 403:
            raise github.BadCredentialsException(
                status=response.status_code,
                data=response.text
            )
        elif response.status_code == 404:
            raise github.UnknownObjectException(
                status=response.status_code,
                data=response.text
            )
        raise github.GithubException(
            status=response.status_code,
            data=response.text
        )


def get_installation_id(integration, owner):
    installations = get_installations(integration)
    for install in installations:
        if install["account"]["login"].lower() == owner.lower():
            return install["id"]


def get_subscription(r, installation_id):
    sub = r.get("subscription-cache-%s" % installation_id)
    if not sub:
        LOG.info("Subscription for %s not cached, retrieving it..." %
                 installation_id)
        resp = requests.get(config.SUBSCRIPTION_URL %
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
            sub["token"] = sub["token"]["access_token"]
            del sub["subscription"]
        else:
            # NOTE(sileht): handle this better
            resp.raise_for_status()
        r.set("subscription-cache-%s" % installation_id, json.dumps(sub),
              ex=60*60)
    else:
        sub = json.loads(sub)

    filtered_sub = sub.copy()
    if filtered_sub["token"]:
        filtered_sub["token"] = "*******"
    LOG.info("Subscription for installation %s: %s", installation_id,
             filtered_sub)
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
