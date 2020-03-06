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

import re
from urllib import parse

import cachecontrol
from cachecontrol.caches import RedisCache
from cachecontrol.serialize import Serializer
import celery.exceptions
import daiquiri
from datadog import statsd
import requests
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.redis import RedisIntegration
import urllib3

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import utils


LOG = daiquiri.getLogger(__name__)


def fixup_sentry_reporting(event, hint):  # pragma: no cover
    # NOTE(sileht): Block exceptions that celery will retry until
    # the retries handler manually send the event.
    is_celery_task = "celery-job" in event.get("extra", {})
    is_exception = "exc_info" in hint
    if is_exception and is_celery_task:
        exc_type, exc_value, tb = hint["exc_info"]

        backoff = exceptions.need_retry(exc_value)
        if backoff or isinstance(exc_value, celery.exceptions.Retry):
            return None

    return event


if config.SENTRY_URL:  # pragma: no cover
    sentry_sdk.init(
        config.SENTRY_URL,
        environment=config.SENTRY_ENVIRONMENT,
        before_send=fixup_sentry_reporting,
        integrations=[CeleryIntegration(), FlaskIntegration(), RedisIntegration()],
    )


RETRY = urllib3.Retry(
    total=None,
    redirect=3,
    connect=5,
    read=5,
    status=5,
    backoff_factor=0.2,
    status_forcelist=list(range(500, 599)) + [429],
    method_whitelist=[
        "HEAD",
        "TRACE",
        "GET",
        "PUT",
        "OPTIONS",
        "DELETE",
        "POST",
        "PATCH",
    ],
    raise_on_status=False,
)


SINGLE_PULL_API_RE = re.compile(
    f"^https://api.{config.GITHUB_DOMAIN}:443/repos/[^/]+/[^/]+/pulls/\\d+$"
)

real_session_init = requests.sessions.Session.__init__


class CustomSerializer(Serializer):
    def prepare_response(self, request, cached):
        cached.get("vary", {}).pop("Authorization")
        return super().prepare_response(request, cached)


class CustomCacheControlAdapter(cachecontrol.CacheControlAdapter):
    def send(self, request, *args, **kwargs):
        # NOTE(sileht): never ever cache get_pull(), we need mergeable_state
        # up2date and when it changes etag is not updated...
        if SINGLE_PULL_API_RE.match(request.url):
            cache_url = self.controller.cache_url(request.url)
            self.cache.delete(cache_url)
        resp = super().send(request, *args, **kwargs)
        hostname = parse.urlsplit(request.url).hostname
        if getattr(resp, "from_cache", False):
            statsd.increment("engine.http_client_cache.hit", tags=[f"to:{hostname}"])
        else:
            statsd.increment("engine.http_client_cache.miss", tags=[f"to:{hostname}"])
        return resp


def cached_session_init(self, *args, **kwargs):
    real_session_init(self, *args, **kwargs)

    if config.HTTP_CACHE_URL:
        adapter = CustomCacheControlAdapter(
            max_retries=RETRY,
            cache=RedisCache(utils.get_redis_for_http_cache()),
            serializer=CustomSerializer(),
        )
    else:
        adapter = requests.adapters.HTTPAdapter(max_retries=RETRY)

    self.mount(f"https://api.{config.GITHUB_DOMAIN}", adapter)
    self.mount(config.SUBSCRIPTION_URL, adapter)
    if config.WEBHOOK_APP_FORWARD_URL:
        self.mount(config.WEBHOOK_APP_FORWARD_URL, adapter)
    if config.WEBHOOK_MARKETPLACE_FORWARD_URL:
        self.mount(config.WEBHOOK_MARKETPLACE_FORWARD_URL, adapter)


requests.sessions.Session.__init__ = cached_session_init
