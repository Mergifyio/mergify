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

import celery
from celery import signals

import daiquiri
from datadog import statsd

from mergify_engine import config
from mergify_engine import exceptions
from mergify_engine import utils

LOG = daiquiri.getLogger(__name__)


app = celery.Celery()

app.conf.broker_url = config.CELERY_BROKER_URL

# Limit the connection to Redis to 1 per worker
app.conf.broker_pool_limit = 1

# Configure number of worker via env var
app.conf.worker_concurrency = int(os.environ.get("CELERYD_CONCURRENCY", 1))

# Enable some monitoring stuffs
app.conf.worker_send_task_events = True

app.conf.task_routes = ([("mergify_engine.tasks.*", {"queue": "mergify"})],)

# User can put regexes in their configuration, since it possible to create
# malicious regexes that take a lot of time to evaluate limit the time a task
# can take
app.conf.task_soft_time_limit = 1 * 60
app.conf.task_time_limit = 2 * 60
# FIXME(sileht): Backport is using get_pulls() that always returns ton of PRs.
app.conf.task_soft_time_limit = 5 * 60
app.conf.task_time_limit = 10 * 60


@signals.setup_logging.connect
def celery_logging(**kwargs):  # pragma: no cover
    utils.setup_logging()


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        60.0,
        queue.smart_strict_workflow_periodic_task.s(),
        name="smart strict workflow",
    )


@signals.task_failure.connect
def retry_task_on_exception(
    sender, task_id, exception, args, kwargs, traceback, einfo, **other
):  # pragma: no cover
    backoff = exceptions.need_retry(exception)

    if backoff is None:
        return

    LOG.warning("job %s: failed %d times - retrying", task_id, sender.request.retries)
    # Exponential ^3 backoff
    retry_in = 3 ** sender.request.retries * backoff
    sender.retry(countdown=retry_in)


@celery.signals.after_task_publish.connect
def statsd_after_task_publish(sender, **kwargs):
    statsd.increment("celery.queue", tags=[f"task_name:{sender}", "service:celery"])


@celery.signals.task_postrun.connect
def statsd_task_postrun(sender, **kwargs):
    statsd.increment(
        "celery.task_run", tags=[f"task_name:{sender.name}", "service:celery"]
    )


@celery.signals.task_failure.connect
def statsd_task_failure(sender, **kwargs):
    statsd.increment(
        "celery.task_failure", tags=[f"task_name:{sender.name}", "service:celery"]
    )


@celery.signals.task_success.connect
def statsd_task_success(sender, **kwargs):
    statsd.increment(
        "celery.task_success", tags=[f"task_name:{sender.name}", "service:celery"]
    )


@celery.signals.task_retry.connect
def statsd_task_retry(sender, **kwargs):
    statsd.increment(
        "celery.task_retry", tags=[f"task_name:{sender.name}", "service:celery"]
    )


@celery.signals.task_unknown.connect
def statsd_task_unknown(sender, **kwargs):
    statsd.increment("celery.task_unknown", tags=["service:celery"])


@celery.signals.task_rejected.connect
def statsd_task_rejected(sender, **kwargs):
    statsd.increment("engine.task_rejected")


# Register our tasks
import mergify_engine.tasks.forward_events  # noqa
import mergify_engine.tasks.github_events  # noqa
import mergify_engine.tasks.mergify_events  # noqa
from mergify_engine.actions.merge import queue  # noqa
