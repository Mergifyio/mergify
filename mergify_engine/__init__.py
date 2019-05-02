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

import daiquiri

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.flask import FlaskIntegration

from mergify_engine import config
from mergify_engine import exception

LOG = daiquiri.getLogger(__name__)


def fixup_sentry_reporting(event, hint):
    # NOTE(sileht): Block exceptions that celery will retry until
    # the retries handler manually send the event.
    is_celery_task = "celery-job" in event.get("extra", {})
    is_exception = 'exc_info' in hint
    if is_exception and is_celery_task:
        exc_type, exc_value, tb = hint['exc_info']
        backoff = exception.need_retry(exc_value)
        if backoff and not getattr(exc_value, "retries_done", False):
            return None

    return event


if config.SENTRY_URL:
    sentry_sdk.init(config.SENTRY_URL,
                    before_send=fixup_sentry_reporting,
                    integrations=[CeleryIntegration(),
                                  FlaskIntegration()])
