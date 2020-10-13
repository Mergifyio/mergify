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

from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

from mergify_engine import config
from mergify_engine import logs
from mergify_engine.web import app as application  # noqa


if config.SENTRY_URL:
    application = SentryAsgiMiddleware(application)  # type: ignore

logs.setup_logging()
