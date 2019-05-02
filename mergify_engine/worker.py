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

from sentry_sdk import capture_exception

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


app = celery.Celery()

app.conf.broker_url = config.CELERY_BROKER_URL

# Enable some monitoring stuffs
app.conf.worker_send_task_events = True

app.conf.task_routes = ([
    ('mergify_engine.tasks.*', {'queue': 'mergify'})
],)


@signals.setup_logging.connect
def celery_logging(**kwargs):  # pragma: no cover
    utils.setup_logging()


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(60.0,
                             merge.smart_strict_workflow_periodic_task.s(),
                             name='v2 smart strict workflow')


MAX_RETRIES = 10


@signals.task_failure.connect
def retry_task_on_exception(sender, task_id, exception, args, kwargs,
                            traceback, einfo, **other):  # pragma: no cover
    backoff = exceptions.need_retry(exception)

    if backoff is None:
        return

    elif sender.request.retries >= MAX_RETRIES:
        LOG.warning('task %s: failed too many times times - moving to '
                    'failed queue', task_id)
        # NOTE(sileht): We inject this attribute so sentry event hook
        # known it can the exception to its backend.
        exception.retries_done = True
        capture_exception(exception)

    else:
        LOG.warning('job %s: failed %d times - retrying',
                    task_id, sender.request.retries)
        # Exponential backoff
        retry_in = 2 ** sender.request.retries * backoff
        sender.retry(countdown=retry_in)


# Register our tasks
import mergify_engine.tasks.github_events  # noqa
from mergify_engine.actions import merge  # noqa
