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

import celery
from celery import signals

import daiquiri

import github

import requests

from mergify_engine import config
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


app = celery.Celery()

app.conf.broker_url = config.CELERY_BROKER_URL

# NOTE(sileht): No local concurrency for now as the engine v1 doesn't support
# it. Events are sharded with a hashring between worker to ensure one
# repository is always handled by the same worker.
app.conf.worker_concurrency = 1

# Enable some monitoring stuffs
app.conf.worker_send_task_events = True

# Create an additional queue per worker
app.conf.worker_direct = True


@signals.setup_logging.connect
def celery_logging(**kwargs):
    utils.setup_logging()
    config.log()


MAX_RETRIES = 10


@signals.task_failure.connect
def retry_task_on_exception(sender, task_id, exception, args, kwargs,
                            traceback, einfo, **other):
    if ((isinstance(exception, github.GithubException) and
         exception.status >= 500) or
            (isinstance(exception, requests.exceptions.HTTPError) and
             exception.response.status_code >= 500) or
            isinstance(exception, requests.exceptions.ConnectionError)):
        backoff = 5

    elif (exception == github.GithubException and
          exception.status == 403 and
          "You have triggered an abuse detection mechanism" in
          exception.data["message"]):
        backoff = 5

    else:
        return

    if sender.request.retries >= MAX_RETRIES:
        LOG.warning('task %s: failed too many times times - moving to '
                    'failed queue', task_id)
    else:
        LOG.warning('job %s: failed %d times - retrying',
                    task_id, sender.request.retries)
        # Exponential backoff
        retry_in = 2 ** sender.request.retries * backoff
        sender.retry(countdown=retry_in)


sentry_client = utils.get_sentry_client()

if sentry_client:
    from raven.contrib.celery import register_signal
    from raven.contrib.celery import register_logger_signal
    register_logger_signal(sentry_client)
    register_signal(sentry_client)

# Register our tasks
import mergify_engine.tasks.github_events  # noqa
