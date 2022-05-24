# -*- encoding: utf-8 -*-
#
# Copyright © 2019 Mehdi Abaakouk <sileht@sileht.net>
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


from unittest import mock

from datadog import statsd

from mergify_engine import worker
from mergify_engine.clients import github_app


# NOTE(sileht): Here the permission that's differ from testing app and production app
github_app.EXPECTED_MINIMAL_PERMISSIONS["User"]["statuses"] = "write"
github_app.EXPECTED_MINIMAL_PERMISSIONS["Organization"]["statuses"] = "write"


statsd.socket = mock.Mock()  # type:ignore[assignment]


worker.WORKER_PROCESSING_DELAY = 0.01
