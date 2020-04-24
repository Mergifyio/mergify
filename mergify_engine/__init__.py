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
import os

import celery.exceptions
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.redis import RedisIntegration

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import logs


LOG = logs.getLogger(__name__)


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
        release=os.environ.get("HEROKU_RELEASE_VERSION"),
        environment=config.SENTRY_ENVIRONMENT,
        before_send=fixup_sentry_reporting,
        integrations=[CeleryIntegration(), RedisIntegration()],
    )
