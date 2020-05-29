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

import asyncio
from concurrent import futures
import os

from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

from mergify_engine import config
from mergify_engine import logs
from mergify_engine.web import app as application  # noqa


if config.SENTRY_URL:
    application = SentryAsgiMiddleware(application)

# Python 3.7 sets max_worker to high by default;
# Python 3.8 has a better default
asyncio.get_event_loop().set_default_executor(
    futures.ThreadPoolExecutor(max_workers=os.environ.get("THREAD_CONCURRENCY", 8),),
)
logs.setup_logging(worker="asgi")
