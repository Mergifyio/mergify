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

import os

import raven.contrib.flask

from mergify_engine import utils
from mergify_engine.web import app as application


sentry_client = utils.prepare_service()
if sentry_client:
    raven.contrib.flask.Sentry(application, client=sentry_client).init_app(
        application)

if 'prometheus_multiproc_dir' in os.environ:
    print("Enabling Prometheus support.")

    import atexit

    # needed to load help string of metrics
    from mergify_engine import stats  # noqa

    import prometheus_client
    from prometheus_client import multiprocess

    from werkzeug.wsgi import DispatcherMiddleware

    registry = prometheus_client.CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    prometheus_app = prometheus_client.make_wsgi_app(registry)

    # TODO(sileht): Add credentials for /metrics
    application = DispatcherMiddleware(
        application, {'/metrics': prometheus_app}
    )

    atexit.register(lambda: stats.cleanup(os.getpid()))
