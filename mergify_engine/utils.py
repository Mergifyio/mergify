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

import celery.app.log

import daiquiri

import github

import redis

import requests

from mergify_engine import config

LOG = daiquiri.getLogger(__name__)


global REDIS_CONNECTION_RQ
REDIS_CONNECTION_RQ = None

global REDIS_CONNECTION_CACHE
REDIS_CONNECTION_CACHE = None


def get_redis_for_cache():
    global REDIS_CONNECTION_CACHE
    if REDIS_CONNECTION_CACHE is None:
        REDIS_CONNECTION_CACHE = redis.StrictRedis.from_url(
            config.STORAGE_URL, decode_responses=True)
    return REDIS_CONNECTION_CACHE


def utcnow():
    return datetime.datetime.now(tz=datetime.timezone.utc)


class CustomFormatter(daiquiri.formatter.ColorExtrasFormatter,
                      celery.app.log.TaskFormatter):
    pass


CELERY_EXTRAS_FORMAT = (
    "%(asctime)s [%(process)d] %(color)s%(levelname)-8.8s "
    "[%(task_id)s] "
    "%(name)s%(extras)s: %(message)s%(color_stop)s"
)


def setup_logging():
    daiquiri.setup(
        outputs=[daiquiri.output.Stream(
            sys.stdout, formatter=CustomFormatter(
                fmt=CELERY_EXTRAS_FORMAT))
        ],
        level=(logging.DEBUG if config.DEBUG else logging.INFO),
    )
    daiquiri.set_default_log_levels([
        ("celery", "INFO"),
        ("kombu", "WARN"),
        ("github.Requester", "WARN"),
        ("urllib3.connectionpool", "WARN"),
        ("vcr", "WARN"),
    ])
    config.log()


def compute_hmac(data):
    mac = hmac.new(config.WEBHOOK_SECRET.encode("utf8"),
                   msg=data, digestmod=hashlib.sha1)
    return str(mac.hexdigest())


def get_installations(integration):  # pragma: no cover
    # FIXME(sileht): This is currently always mocked in tests

    installs = []
    url = "https://api.%s/app/installations" % config.GITHUB_DOMAIN
    token = "Bearer {}".format(integration.create_jwt())
    session = requests.Session()
    while True:
        response = session.get(url, headers={
            "Authorization": token,
            "Accept": "application/vnd.github.machine-man-preview+json",
            "User-Agent": "PyGithub/Python"
        })
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


def get_installation_token(installation_id):
    integration = github.GithubIntegration(config.INTEGRATION_ID,
                                           config.PRIVATE_KEY)
    try:
        return integration.get_access_token(installation_id).token
    except github.UnknownObjectException:  # pragma: no cover
        LOG.error("token for install %d does not exists anymore",
                  installation_id)
        return


class Gitter(object):
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="mergify-gitter")
        LOG.info("working in: %s", self.tmp)

    def __call__(self, *args, **kwargs):  # pragma: no cover
        LOG.info("calling: %s", " ".join(args))
        kwargs["cwd"] = self.tmp
        kwargs["stderr"] = subprocess.STDOUT
        return subprocess.check_output(["git"] + list(args), **kwargs)

    def cleanup(self):
        LOG.info("cleaning: %s", self.tmp)
        try:
            self("credential-cache",
                 "--socket=%s/.git/creds/socket" % self.tmp,
                 "exit")
        except subprocess.CalledProcessError:  # pragma: no cover
            LOG.warning("git credential-cache exit fail")
        shutil.rmtree(self.tmp)

    def configure(self):
        self("config", "user.name", "%s-bot" % config.CONTEXT)
        self("config", "user.email", config.GIT_EMAIL)
        # Use one git cache daemon per Gitter
        self("config", "credential.useHttpPath", "true")
        self("config", "credential.helper",
             "cache --timeout=300 --socket=%s/.git/creds/socket" % self.tmp)

    def add_cred(self, username, password, path):
        domain = config.GITHUB_DOMAIN
        self("credential", "approve",
             input=("url=https://%s:%s@%s/%s\n\n" %
                    (username, password, domain, path)).encode("utf8"))
