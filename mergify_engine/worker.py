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
from mergify_engine import mergify_pull
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


app = celery.Celery()

app.conf.broker_url = config.CELERY_BROKER_URL

# Enable some monitoring stuffs
app.conf.worker_send_task_events = True

# NOTE(sileht) names the v1 engine queue, so we can run tasks into one
# worker/one thread.
app.conf.task_routes = {'tasks.engine.v1.*': {'queue': 'legacy-engine-v1'}}


@signals.setup_logging.connect
def celery_logging(**kwargs):  # pragma: no cover
    utils.setup_logging()
    config.log()


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(60.0,
                             merge.smart_strict_workflow_periodic_task.s(),
                             name='v2 smart strict workflow')


MAX_RETRIES = 10


@signals.task_failure.connect
def retry_task_on_exception(sender, task_id, exception, args, kwargs,
                            traceback, einfo, **other):  # pragma: no cover
    if isinstance(exception, mergify_pull.MergeableStateUnknown):
        backoff = 30
    elif ((isinstance(exception, github.GithubException) and
           exception.status >= 500) or
          (isinstance(exception, requests.exceptions.HTTPError) and
           exception.response.status_code >= 500) or
          isinstance(exception, requests.exceptions.ConnectionError)):
        backoff = 30

    elif (isinstance(exception, github.GithubException) and
          exception.status == 403 and
          ("You have triggered an abuse detection mechanism" in
           exception.data["message"] or
           exception.data["message"].startswith("API rate limit exceeded"))
          ):
        backoff = 60 * 5
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

if sentry_client:  # pragma: no cover
    from raven.contrib.celery import register_signal
    from raven.contrib.celery import register_logger_signal
    register_logger_signal(sentry_client)
    register_signal(sentry_client)

# Register our tasks
import mergify_engine.tasks.github_events  # noqa
from mergify_engine.actions import merge  # noqa
